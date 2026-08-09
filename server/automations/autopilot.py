"""The autopilot automation: keeps governed sessions moving through their stuck
states until their effective goal is met. Metadata + config here; the control loop
(AutopilotLoop) is added in a later task."""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from .autopilot_policy import (
    Decision, DecisionContext, fingerprint, post_validate, pre_decide,
)
from .base import AutomationStatus, ConfigField

log = logging.getLogger(__name__)

_DAY = 24 * 3600.0


class Autopilot:
    id = "autopilot"
    name = "Autopilot"
    description = "Continuously advances every governed session toward its goal."
    modes: tuple[str, ...] = ("off", "propose", "auto")
    config_schema = [
        ConfigField(key="allow_tool_approvals", label="Approve tool permissions", type="bool",
                    default=True, help="If off, autopilot never approves permissions (only "
                                       "denies / answers / nudges)."),
        ConfigField(key="max_actions_per_session_per_hour", label="Max actions/session/hour",
                    type="int", default=12, help="Per-session circuit-breaker."),
        ConfigField(key="max_actions_per_day", label="Max actions/day (global)", type="int",
                    default=300, help="Global rolling-24h circuit-breaker."),
        ConfigField(key="recovery_max_attempts", label="Max recovery attempts", type="int",
                    default=2, help="Recovery nudges before handing a failed session to a human."),
        ConfigField(key="loop_repeat_limit", label="Loop repeat limit", type="int", default=3,
                    help="Identical stuck states before escalating to a human."),
        ConfigField(key="human_defer_seconds", label="Human deference (seconds)", type="int",
                    default=120, help="Skip a session a human touched this recently."),
        ConfigField(key="extra_deny_patterns", label="Extra deny patterns", type="string",
                    default="", help="Comma-separated patterns that EXTEND (never shrink) the "
                                     "built-in destructive-command deny-list."),
        ConfigField(key="enabled_repos", label="Repositories", type="repo-multiselect",
                    default=[], help="Limit governance to these repos (empty = all)."),
    ]

    def status(self, ctx) -> AutomationStatus:
        counts = getattr(ctx, "autopilot_counts", None) if ctx is not None else None
        if not counts:
            return AutomationStatus(text="idle", level="idle")
        advancing = counts.get("advancing", 0)
        escalated = counts.get("escalated", 0)
        return AutomationStatus(
            text=f"{advancing} advancing" + (f", {escalated} escalated" if escalated else ""),
            level="active" if advancing else ("warn" if escalated else "idle"),
            counts=counts)

    async def on_state_change(self, *, enabled, mode, config, ctx) -> None:
        # The orchestrator's loop reads mode/config each tick; nothing to start here.
        ...


class AutopilotLoop:
    """Restart-safe, non-reentrant control loop. Every I/O dependency is injected so
    the whole decide/apply pipeline is testable with no network, model, or sessions.
    tick() runs under a single lock — an interval tick and a status nudge must never
    overlap (a session could otherwise be double-acted)."""

    def __init__(self, ctx, *,
                 read_config: Callable[[], Awaitable[tuple[str, dict]]],
                 list_governed: Callable[[], Awaitable[list[str]]],
                 get_context: Callable[[str], Awaitable[DecisionContext | None]],
                 apply: Callable[[str, Decision, "DecisionContext | None"], Awaitable[bool]],
                 decide_fn: Callable[[DecisionContext], Awaitable[Decision]],
                 clock: Callable[[], float],
                 archive: Callable[[str], Awaitable[bool]] | None = None,
                 compose: "Callable[[DecisionContext, Decision], Awaitable[str | None]] | None" = None,
                 refresh_facts: "Callable[[str], Awaitable[object | None]] | None" = None) -> None:
        self._ctx = ctx
        self._read_config = read_config
        self._list_governed = list_governed
        self._get_context = get_context
        self._apply = apply
        self._decide_fn = decide_fn
        self._clock = clock
        self._archive = archive          # goal met → archive the session (auto mode)
        self._compose = compose          # Sonnet nudge composer (None = legacy pass-through)
        self._refresh_facts = refresh_facts  # on-demand PR ground-truth refresh
        self._lock = asyncio.Lock()
        self._day_actions: list[float] = []
        self._circuit_emitted = False

    async def tick(self) -> None:
        async with self._lock:
            try:
                await self._run()
            except Exception:  # pragma: no cover — a tick must never crash the loop
                log.warning("autopilot.tick_failed", exc_info=True)

    def _prune_day(self, now: float) -> None:
        self._day_actions = [t for t in self._day_actions if t > now - _DAY]

    async def _run(self) -> None:
        mode, config = await self._read_config()
        if mode == "off":
            return
        now = self._clock()
        self._prune_day(now)
        advancing = escalated = 0
        for sid in await self._list_governed():
            try:
                acted, esc = await self._advance_one(sid, mode, config, now)
                advancing += int(acted)
                escalated += int(esc)
            except Exception:  # one session must never stall the tick
                log.warning("autopilot.session_failed sid=%s", sid, exc_info=True)
        setattr(self._ctx, "autopilot_counts", {"advancing": advancing, "escalated": escalated})

    async def _advance_one(self, sid: str, mode: str, config: dict, now: float) -> tuple[bool, bool]:
        ctx = await self._get_context(sid)
        if ctx is None:
            return False, False
        ledger = await self._ctx.get_target_state(sid)
        # Merge stored ledger with whatever get_context already loaded: stored state
        # is authoritative for persisted keys, but get_context may carry in-memory
        # keys (e.g. recent_nudges injected by a test or a warm-path shortcut) that
        # are not yet persisted. Merge with stored taking precedence over pre-loaded,
        # so we never silently discard a freshly persisted key.
        ctx.ledger = {**(ctx.ledger or {}), **ledger}

        # Global daily cap.
        if len(self._day_actions) >= config["max_actions_per_day"]:
            if not self._circuit_emitted:
                self._circuit_emitted = True
                await self._ctx.emit("warn", "daily action cap reached — pausing",
                                     kind="circuit_open")
            return False, False
        self._circuit_emitted = False

        decision = pre_decide(ctx, config, now)
        if decision is None:
            decision = post_validate(await self._decide_fn(ctx), ctx, config)
        if decision.action == "nudge":
            decision = await self._verify_nudge_facts(sid, ctx, decision, now)
        if decision.action == "skip":
            await self._record_fingerprint(sid, ledger, ctx)
            return False, False
        if decision.action == "complete":
            # A human unarchived this session — it must NEVER be auto-archived again
            # (the user's standing decision outranks the goal-met signal). Surface the
            # met goal once, then stop; autopilot keeps helping it in every other way.
            if ctx.no_auto_archive:
                return await self._decline_archive(sid, ledger, ctx, decision)
            # The goal is met — this session is finished. In auto mode archive it
            # (reversible; unarchive exists) via the archive seam — NEVER via apply,
            # which stays reserved for validated in-session actions. In propose mode
            # only surface the proposal. Either way the loop stops advancing it.
            if mode == "propose":
                await self._ctx.emit(
                    "info", f"{sid}: propose archive — goal met: {decision.reason}",
                    kind="proposed", session_id=sid,
                    detail={"action": "complete", "reason": decision.reason})
                return False, False
            await self._ctx.emit("info", f"{sid}: goal met — {decision.reason}",
                                 kind="complete", session_id=sid)
            if self._archive is not None:
                try:
                    await self._archive(sid)
                    # Terminal rest: drop the ledger row so the table stays bounded
                    # (mirrors the backlog runner deleting target-state on merge).
                    await self._ctx.delete_target_state(sid)
                except Exception:  # best-effort — a failed archive must not crash the tick
                    log.warning("autopilot.archive_failed sid=%s", sid, exc_info=True)
            return False, False
        if decision.action == "escalate":
            await self._escalate(sid, ledger, ctx, decision)
            return False, True

        if mode == "propose":
            await self._ctx.emit("info", f"{sid}: propose {decision.action} — {decision.reason}",
                                 kind="proposed", session_id=sid,
                                 detail={"action": decision.action, "reason": decision.reason})
            return False, False

        if decision.action == "nudge":
            decision = await self._compose_nudge_decision(sid, ctx, decision, now)
            if decision.action == "suppressed":
                return False, False
            if decision.action == "escalate":
                await self._escalate(sid, ctx.ledger if ctx.ledger is not None else ledger, ctx, decision)
                return False, True

        # Pass the DECIDE-TIME context to apply so it resolves exactly the
        # permission/question that pre_decide + post_validate just validated — never
        # a freshly re-read one (which could be a new, un-vetted pending permission
        # the session surfaced during the decider's await window).
        acted = await self._apply(sid, decision, ctx)
        if acted:
            self._day_actions.append(now)
            await self._charge(sid, ctx.ledger if ctx.ledger is not None else ledger, ctx, decision, now)
            await self._ctx.emit("info", f"{sid}: {decision.action} — {decision.reason}",
                                 kind="acted", session_id=sid,
                                 detail={"action": decision.action})
        else:
            # The action did not mutate (e.g. a nudge that never landed). Still
            # accumulate the stuck-state fingerprint so loop detection can escalate
            # a session that would otherwise be nudged forever without progress.
            await self._record_fingerprint(sid, ctx.ledger if ctx.ledger is not None else ledger, ctx)
        return acted, False

    async def _decline_archive(self, sid: str, ledger: dict, ctx: DecisionContext,
                               decision: Decision) -> tuple[bool, bool]:
        """Goal met, but the session was manually unarchived → never auto-archive it.
        Surface the completion ONCE (deduped on the stuck fingerprint, mirroring the
        escalate de-spam) so the feed isn't buried every tick, and stop advancing it."""
        fp = fingerprint(ctx)
        if ledger.get("archive_declined") and ledger.get("last_fingerprint") == fp:
            return False, False
        new = dict(ledger)
        new.update(archive_declined=True, last_fingerprint=fp)
        await self._ctx.set_target_state(sid, new)
        await self._ctx.emit(
            "info", f"{sid}: goal met — not auto-archiving (you unarchived this)",
            kind="proposed", session_id=sid,
            detail={"action": "complete", "reason": decision.reason,
                    "suppressed": "manual-unarchive"})
        return False, False

    async def _record_fingerprint(self, sid: str, ledger: dict, ctx: DecisionContext) -> None:
        fp = fingerprint(ctx)
        repeat = ledger.get("repeat_count", 0) + 1 if ledger.get("last_fingerprint") == fp else 1
        new = dict(ledger)
        new.update(last_fingerprint=fp, repeat_count=repeat)
        await self._ctx.set_target_state(sid, new)

    async def _charge(self, sid: str, ledger: dict, ctx: DecisionContext,
                      decision: Decision, now: float) -> None:
        fp = fingerprint(ctx)
        repeat = ledger.get("repeat_count", 0) + 1 if ledger.get("last_fingerprint") == fp else 1
        hour = [t for t in ledger.get("hour_actions", []) if t > now - 3600.0] + [now]
        recovery = ledger.get("recovery_attempts", 0)
        if ctx.stuck.get("kind") == "needs_attention" and decision.action == "nudge":
            recovery += 1
        recent = list(ledger.get("recent_nudges", []))
        if decision.action == "nudge" and (decision.prompt or "").strip():
            from .autopilot_policy import RECENT_NUDGES_CAP
            recent = (recent + [{"ts": now, "text": decision.prompt}])[-RECENT_NUDGES_CAP:]
        new = dict(ledger)
        new.update(hour_actions=hour, last_fingerprint=fp, repeat_count=repeat,
                   recovery_attempts=recovery,
                   recent_nudges=recent,
                   # A successful action means the session is being advanced again —
                   # it is no longer "handed to a human", so a later escalation
                   # (even for the same stuck state) is fresh information.
                   escalated=False)
        await self._ctx.set_target_state(sid, new)

    async def _verify_nudge_facts(self, sid: str, ctx: DecisionContext,
                                  decision: Decision, now: float) -> Decision:
        """Verify-before-directing (spec §2): when the cached PR facts contradict
        the session's claims (or a self-declared complete), refresh ground truth
        once per FACTS_REFRESH_MIN_INTERVAL_S and re-judge. A goal verified met
        restores the original `complete` — the nudge never happens."""
        from .autopilot_policy import FACTS_REFRESH_MIN_INTERVAL_S, facts_contradicted
        if self._refresh_facts is None or not facts_contradicted(ctx):
            return decision
        ledger = ctx.ledger or {}
        if now - float(ledger.get("last_facts_refresh", 0.0)) < FACTS_REFRESH_MIN_INTERVAL_S:
            return decision
        new = dict(ledger)
        new["last_facts_refresh"] = now
        await self._ctx.set_target_state(sid, new)
        ctx.ledger = new
        fresh = await self._refresh_facts(sid)
        if fresh is None:
            return decision          # cooldown/failure: compose against stale facts
        ctx.pr_facts = fresh
        ctx.facts_verified = True
        if decision.downgraded_from == "complete":
            from .completion_bar import met_bar
            verdict = met_bar(ctx.effective_goal, ctx.goal_source, fresh,
                              ctx.pr_resolution, instructions=ctx.instructions)
            if verdict.met:
                return Decision(action="complete", reason=f"verified: {verdict.reason}")
        return decision

    async def _compose_nudge_decision(self, sid: str, ctx: DecisionContext,
                                      decision: Decision, now: float) -> Decision:
        """Compose the final nudge text (spec §3) and dedupe it (spec §4). No
        composer: a gate-authored prompt passes through (legacy); a composition-
        marked decision without text escalates — never a template."""
        from .autopilot_policy import near_duplicate
        if self._compose is None:
            if (decision.prompt or "").strip():
                return decision
            return Decision(action="escalate", reason="nudge needs composition — no composer")
        text = await self._compose(ctx, decision)
        if not (text or "").strip():
            return Decision(action="escalate",
                            reason=f"composer declined/failed ({decision.reason})")
        if near_duplicate(text, (ctx.ledger or {}).get("recent_nudges", []), now):
            await self._record_suppressed(sid, ctx.ledger or {}, ctx)
            return Decision(action="suppressed", reason="near-duplicate nudge suppressed")
        return Decision(action="nudge", prompt=text, reason=decision.reason,
                        downgraded_from=decision.downgraded_from)

    async def _record_suppressed(self, sid: str, ledger: dict, ctx: DecisionContext) -> None:
        """A suppressed near-duplicate counts toward loop escalation UNCONDITIONALLY —
        repetition here is keyed on what autopilot said, immune to the goal-text
        drift that resets `fingerprint` (spec §4)."""
        new = dict(ledger)
        new.update(last_fingerprint=fingerprint(ctx),
                   repeat_count=ledger.get("repeat_count", 0) + 1)
        await self._ctx.set_target_state(sid, new)

    async def _escalate(self, sid: str, ledger: dict, ctx: DecisionContext,
                        decision: Decision) -> None:
        # De-spam: a session already handed to a human for THIS stuck state stays
        # escalated silently — re-emitting "needs human" every tick buries the feed.
        # A changed fingerprint is new information and emits again.
        fp = fingerprint(ctx)
        if ledger.get("escalated") and ledger.get("last_fingerprint") == fp:
            return
        new = dict(ledger)
        new.update(escalated=True, last_fingerprint=fp)
        await self._ctx.set_target_state(sid, new)
        await self._ctx.emit("warn", f"{sid}: needs human — {decision.reason}",
                             kind="escalated", session_id=sid)
