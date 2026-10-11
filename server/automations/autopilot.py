"""The autopilot automation: keeps governed sessions moving through their stuck
states until their effective goal is met. Metadata + config here; the control loop
(AutopilotLoop) is added in a later task."""
from __future__ import annotations

from .context import observation_failed, publish_observation

import asyncio
import logging
from typing import Awaitable, Callable, cast

from server.sessions.helper_call import HelperCredentialsPending

from .autopilot_policy import (
    Decision, DecisionContext, fingerprint, post_validate, pre_decide,
)
from .base import AutomationStatus, ConfigField
from .engine_policy import EnginePolicy

log = logging.getLogger(__name__)


class _StaleDecision(Exception):
    """The session changed owners or turns; leave its durable ledger untouched."""


class Autopilot:
    id = "autopilot"
    name = "Autopilot"
    work_class = "maintenance"
    description = "Continuously advances every governed session toward its goal."
    modes: tuple[str, ...] = ("off", "propose", "auto")
    # Declared for `audited_policy_map()`'s whole-fleet ceiling view, but autopilot
    # never spawns a session — it only acts on sessions the operator already
    # launched, on any engine, through the generic driver seams (approve/deny/
    # answer/nudge). This ceiling therefore gates nothing here; the actual
    # engine-safety boundary is the decider/composer's helper LLM call, which
    # must never reuse a governed session's own account when that account is not
    # Anthropic (`SessionOrchestrator._autopilot_helper_config_dir`, #1262).
    engine_policy: EnginePolicy = EnginePolicy.CLAUDE_ONLY
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
                    help="Repeated assistance attempts before escalating to a human."),
        ConfigField(key="human_defer_seconds", label="Human deference (seconds)", type="int",
                    default=120, help="Skip a session a human touched this recently."),
        ConfigField(key="extra_deny_patterns", label="Extra deny patterns", type="string",
                    default="", help="Comma-separated patterns that EXTEND (never shrink) the "
                                     "built-in destructive-command deny-list."),
        ConfigField(key="enabled_repos", label="Repositories", type="repo-multiselect",
                    default=[], help="Limit governance to these repos (empty = all)."),
        ConfigField(key="auto_archive_completed", label="Auto-archive completed sessions",
                    type="bool", default=True,
                    help="If off, autopilot surfaces a met goal but never archives the "
                         "session — it stays in ready_review for a human."),
        ConfigField(key="auto_archive_origins", label="Auto-archive origins",
                    type="string-list", default=[],
                    help="Only auto-archive sessions spawned by these automations "
                         "(e.g. backlog-runner); empty = any origin."),
    ]

    def status(self, ctx) -> AutomationStatus:
        from .autopilot_observation import autopilot_status
        counts = getattr(ctx, "autopilot_counts", None) if ctx is not None else None
        return autopilot_status(counts)

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
                 archive: Callable[[str, DecisionContext], Awaitable[bool]] | None = None,
                 may_act: Callable[[str, DecisionContext], Awaitable[bool]] | None = None,
                 is_current: Callable[[str, DecisionContext], Awaitable[bool]] | None = None,
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
        self._may_act = may_act
        self._is_current = is_current

    async def tick(self) -> None:
        async with self._lock:
            try:
                await self._run()
            except Exception:  # pragma: no cover — a tick must never crash the loop
                observation_failed(self._ctx)
                log.warning("autopilot.tick_failed", exc_info=True)

    async def _run(self) -> None:
        mode, config = await self._read_config()
        if mode == "off":
            return
        advancing = escalated = 0
        degraded = False
        governed = await self._list_governed()
        counts = {"waiting": 0, "deferring": 0, "budget_blocked": 0,
                  "paused": 0, "handed_back": 0, "pr_facts_unavailable": 0, "policy_paused": 0,
                  "credentials_pending": 0}
        for sid, reason in getattr(self._ctx, "autopilot_withheld", {}).items():
            try:
                context = await self._get_context(sid)
                if context is None:
                    continue
                ledger = await self._ctx.get_target_state(sid)
                await self._observe(sid, ledger, reason, ctx=context)
            except _StaleDecision:
                continue
            counts["waiting"] += 1
            counts[reason] = counts.get(reason, 0) + 1
        for sid in governed:
            try:
                acted, esc = await self._advance_one(sid, mode, config, self._clock())
                advancing += int(acted)
                escalated += int(esc)
                state = await self._ctx.get_target_state(sid)
                reason = state.get("observation", {}).get("reason")
                if reason and reason not in ("advancing", "escalated"):
                    counts["waiting"] += 1
                    if reason in counts and reason != "waiting":
                        counts[reason] += 1
            except _StaleDecision:
                continue
            except Exception:  # one session must never stall the tick
                degraded = True
                log.warning("autopilot.session_failed sid=%s", sid, exc_info=True)
        publish_observation(self._ctx, "autopilot_counts", {**counts, "advancing": advancing, "escalated": escalated},
                            degraded=degraded)

    async def _advance_one(self, sid: str, mode: str, config: dict, now: float) -> tuple[bool, bool]:
        ctx = await self._get_context(sid)
        if ctx is None:
            return False, False
        ledger = await self._ctx.get_target_state(sid)
        await self._assert_current(sid, ctx)
        # Merge stored ledger with whatever get_context already loaded: stored state
        # is authoritative for persisted keys, but get_context may carry in-memory
        # keys (e.g. recent_nudges injected by a test or a warm-path shortcut) that
        # are not yet persisted. Merge with stored taking precedence over pre-loaded,
        # so we never silently discard a freshly persisted key.
        ctx.ledger = {**(ctx.ledger or {}), **ledger}
        await self._reconcile_accounting(sid, ctx, now)
        ledger = ctx.ledger
        if not await self._ctx.autopilot_action_available(config["max_actions_per_day"], self._clock()):
            await self._observe(sid, ledger, "budget_blocked", ctx=ctx)
            return False, False

        decision = pre_decide(ctx, config, now)
        if decision is None:
            decision = await self._verify_facts(sid, ctx, now)
            if decision is None:
                await self._assert_current(sid, ctx)
                decision = post_validate(await self._decide_fn(ctx), ctx, config)
        await self._assert_current(sid, ctx)
        ledger = ctx.ledger
        if decision.action == "skip":
            # Observing a wait is not an assistance attempt. In particular, neither
            # deference nor an exhausted hourly budget consumes the loop allowance.
            reason = decision.waiting_reason or "waiting"
            deadline = None
            if reason == "deferring" and ctx.seconds_since_human is not None:
                deadline = now - ctx.seconds_since_human + config.get("human_defer_seconds", 120)
            elif reason == "budget_blocked" and ledger.get("hour_actions"):
                active = [stamp for stamp in ledger["hour_actions"] if stamp > now - 3600]
                limit = config["max_actions_per_session_per_hour"]
                if limit > 0 and len(active) >= limit:
                    deadline = sorted(active)[len(active) - limit] + 3600
            await self._observe(sid, ledger, reason, ctx=ctx, deadline=deadline)
            return False, False
        if decision.action == "complete":
            decline_ledger = ctx.ledger if ctx.ledger is not None else ledger
            # A human unarchived this session — it must NEVER be auto-archived again
            # (the user's standing decision outranks the goal-met signal). Surface the
            # met goal once, then stop; autopilot keeps helping it in every other way.
            if ctx.no_auto_archive:
                return await self._decline_archive(
                    sid, decline_ledger, ctx, decision,
                    suppressed="manual-unarchive", note="you unarchived this")
            # Operator-level opt-out: auto-archival disabled for the whole automation.
            if not config.get("auto_archive_completed", True):
                return await self._decline_archive(
                    sid, decline_ledger, ctx, decision,
                    suppressed="auto-archive-disabled", note="auto-archive is disabled")
            # Operator-level scoping: only archive sessions spawned by a listed automation.
            archive_origins = config.get("auto_archive_origins") or []
            if archive_origins and ctx.origin not in archive_origins:
                return await self._decline_archive(
                    sid, decline_ledger, ctx, decision,
                    suppressed="origin-excluded", note="origin not in auto-archive scope")
            # The goal is met — this session is finished. In auto mode archive it
            # (reversible; unarchive exists) via the archive seam — NEVER via apply,
            # which stays reserved for validated in-session actions. In propose mode
            # only surface the proposal. Either way the loop stops advancing it.
            if mode == "propose":
                await self._observe(sid, ctx.ledger, "proposed", ctx=ctx)
                await self._ctx.emit(
                    "info", f"{sid}: propose archive — goal met: {decision.reason}",
                    kind="proposed", session_id=sid,
                    detail={"action": "complete", "reason": decision.reason})
                return False, False
            if self._archive is not None:
                try:
                    if not await self._reserve(sid, ctx, config):
                        return False, False
                    if not await self._archive(sid, ctx):
                        return False, False
                    # Terminal rest: drop the ledger row so the table stays bounded
                    # (mirrors the backlog runner deleting target-state on merge).
                    await self._ctx.delete_target_state(sid)
                except Exception:  # best-effort — a failed archive must not crash the tick
                    log.warning("autopilot.archive_failed sid=%s", sid, exc_info=True)
                    return False, False
            await self._ctx.emit("info", f"{sid}: goal met — {decision.reason}",
                                 kind="complete", session_id=sid)
            return False, False
        if decision.action == "escalate":
            await self._escalate(sid, ledger, ctx, decision)
            return False, True

        if mode == "propose":
            await self._observe(sid, ctx.ledger, "proposed", ctx=ctx)
            await self._ctx.emit("info", f"{sid}: propose {decision.action} — {decision.reason}",
                                 kind="proposed", session_id=sid,
                                 detail={"action": decision.action, "reason": decision.reason})
            return False, False

        if decision.action == "nudge":
            if not await self._ctx.autopilot_action_available(config["max_actions_per_day"], self._clock()):
                await self._observe(sid, ctx.ledger, "budget_blocked", ctx=ctx)
                return False, False
            decision = await self._compose_nudge_decision(sid, ctx, decision, now)
            if decision.action == "skip":
                await self._observe(sid, ctx.ledger, decision.waiting_reason or "waiting", ctx=ctx)
                return False, False
            if decision.action == "suppressed":
                await self._observe(sid, ctx.ledger, "waiting", ctx=ctx)
                return False, False
            if decision.action == "escalate":
                await self._escalate(sid, ctx.ledger if ctx.ledger is not None else ledger, ctx, decision)
                return False, True

        # Pass the DECIDE-TIME context to apply so it resolves exactly the
        # permission/question that pre_decide + post_validate just validated — never
        # a freshly re-read one (which could be a new, un-vetted pending permission
        # the session surfaced during the decider's await window).
        if not await self._reserve(sid, ctx, config):
            return False, False
        now = self._clock()
        acted = await self._apply(sid, decision, ctx)
        if acted:
            ctx.action_applied = True
            await self._charge(sid, ctx.ledger if ctx.ledger is not None else ledger, ctx, decision, now)
            await self._observe(sid, ctx.ledger, "advancing", ctx=ctx)
            await self._ctx.emit("info", f"{sid}: {decision.action} — {decision.reason}",
                                 kind="acted", session_id=sid,
                                 detail={"action": decision.action})
        else:
            # The action did not mutate (e.g. a nudge that never landed). Still
            # accumulate the stuck-state fingerprint so loop detection can escalate
            # a session that would otherwise be nudged forever without progress.
            await self._record_fingerprint(sid, ctx.ledger if ctx.ledger is not None else ledger, ctx)
            await self._observe(sid, ctx.ledger, "waiting", ctx=ctx)
        return acted, False

    async def _reserve(self, sid: str, ctx: DecisionContext, config: dict) -> bool:
        if self._may_act is not None and not await self._may_act(sid, ctx):
            # That callback can discover a new owner or turn. Its ledger is now
            # theirs: even recording why we stopped would overwrite their work.
            return False
        if not await self._ctx.reserve_autopilot_action(config["max_actions_per_day"], self._clock()):
            await self._observe(sid, ctx.ledger, "budget_blocked", ctx=ctx)
            return False
        ctx.ledger = {**ctx.ledger, "last_attempt_at": self._clock()}
        await self._store(sid, ctx.ledger, ctx)
        return True

    async def _store(self, sid: str, state: dict, ctx: DecisionContext) -> None:
        """Recheck ownership after awaited model/PR work, before writing its ledger."""
        await self._assert_current(sid, ctx)
        await self._ctx.set_target_state(sid, state)
        ctx.ledger = state

    async def _assert_current(self, sid: str, ctx: DecisionContext) -> None:
        if self._is_current is not None and not await self._is_current(sid, ctx):
            raise _StaleDecision()

    async def _observe(self, sid: str, ledger: dict, reason: str, *,
                       ctx: DecisionContext, deadline: float | None = None) -> None:
        """Publish a wait once per reason/episode, preserving retry and budget state."""
        labels = {"deferring": "waiting for human steering to settle",
                  "budget_blocked": "waiting for the action budget",
                  "paused": "session assistance is paused",
                  "policy_paused": "session assistance is paused by product policy",
                  "handed_back": "waiting for your response",
                  "pr_facts_unavailable": "waiting for verified PR facts",
                  "credentials_pending": "waiting for helper credentials",
                  "proposed": "assistance proposed for review",
                  "archive_paused": "automatic archive is paused",
                  "escalated": "waiting for human review", "waiting": "waiting"}
        episode = ctx.last_human_interaction_at
        previous = ledger.get("observation", {})
        observation = {"reason": reason, "deadline": deadline,
                       "lastAttempt": ledger.get("last_attempt_at"), "episode": episode}
        new = {**ledger, "observation": observation}
        await self._store(sid, new, ctx)
        if reason != "advancing" and (previous.get("reason"), previous.get("episode")) != (reason, episode):
            await self._ctx.emit("info", f"{sid}: {labels.get(reason, 'waiting')}",
                                 kind="waiting", session_id=sid, detail=observation)

    async def _decline_archive(self, sid: str, ledger: dict, ctx: DecisionContext,
                               decision: Decision, *, suppressed: str,
                               note: str) -> tuple[bool, bool]:
        """Goal met, but something suppresses auto-archival (manual unarchive, the
        `auto_archive_completed` config off, or `auto_archive_origins` scoping this
        session's origin out) → surface the completion ONCE (deduped on the stuck
        fingerprint, mirroring the escalate de-spam) and stop advancing it, without
        ever archiving. *suppressed* is the machine-readable reason for the detail
        payload; *note* is the human-readable parenthetical."""
        await self._observe(sid, ledger, "archive_paused", ctx=ctx)
        ledger = ctx.ledger
        fp = fingerprint(ctx)
        if ledger.get("archive_declined") and ledger.get("last_fingerprint") == fp:
            return False, False
        new = dict(ledger)
        new.update(archive_declined=True, last_fingerprint=fp)
        await self._store(sid, new, ctx)
        await self._ctx.emit(
            "info", f"{sid}: goal met — not auto-archiving ({note})",
            kind="proposed", session_id=sid,
            detail={"action": "complete", "reason": decision.reason,
                    "suppressed": suppressed})
        return False, False

    async def _reconcile_accounting(self, sid: str, ctx: DecisionContext, now: float) -> None:
        """Recover legacy observation counters once; human input starts a new episode.

        Legacy ledgers cannot distinguish skips from attempts. Reset only their
        repetition count once, retaining action budgets, recovery caps, recent
        nudges and notification deduplication. Persisting the version makes this
        bounded recovery survive restarts without reopening the allowance again.
        """
        ledger = ctx.ledger
        new = dict(ledger)
        if new.get("clock_version", 0) < 1:
            # Legacy monotonic values have no cross-host/restart meaning. Retain
            # every charge for one conservative window from migration, once only.
            new["hour_actions"] = [now for _ in new.get("hour_actions", [])]
            new["recent_nudges"] = [{**item, "ts": now} for item in new.get("recent_nudges", [])]
            new.pop("last_facts_refresh", None)
            new["clock_version"] = 1
        human_at = ctx.last_human_interaction_at
        if new.get("accounting_version", 0) < 1:
            new.update(accounting_version=1, repeat_count=0)
            # The first observation establishes a baseline; old human activity
            # must not erase an already-exhausted recovery allowance on upgrade.
            if human_at is not None:
                new["last_human_interaction_at"] = human_at
        if human_at is not None and human_at > new.get("last_human_interaction_at", ""):
            new.update(last_human_interaction_at=human_at, repeat_count=0,
                       recovery_attempts=0, escalated=False)
        if new != ledger:
            await self._store(sid, new, ctx)
            ctx.ledger = new

    async def _record_fingerprint(self, sid: str, ledger: dict, ctx: DecisionContext) -> None:
        fp = fingerprint(ctx)
        repeat = ledger.get("repeat_count", 0) + 1 if ledger.get("last_fingerprint") == fp else 1
        new = dict(ledger)
        new.update(last_fingerprint=fp, repeat_count=repeat)
        ctx.ledger = new
        await self._store(sid, new, ctx)

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
        ctx.ledger = new
        await self._store(sid, new, ctx)

    async def _verify_facts(self, sid: str, ctx: DecisionContext,
                            now: float) -> Decision | None:
        """Refresh contradictory evidence before any model judgment, once per cooldown.

        An unavailable refresh is a wait, never proof that the claimed work is
        absent. Hard guardrails have already run, including explicit hand-backs.
        """
        from .autopilot_policy import FACTS_REFRESH_MIN_INTERVAL_S, facts_contradicted
        if self._refresh_facts is None or not facts_contradicted(ctx):
            return None
        ledger = ctx.ledger
        if ("last_facts_refresh" in ledger
                and now - float(ledger["last_facts_refresh"]) < FACTS_REFRESH_MIN_INTERVAL_S):
            return Decision(action="skip", reason="PR facts unavailable — refresh cooldown",
                            waiting_reason="pr_facts_unavailable")
        new = dict(ledger)
        new["last_facts_refresh"] = now
        await self._store(sid, new, ctx)
        ctx.ledger = new
        try:
            fresh_obj = await self._refresh_facts(sid)
        except Exception:
            log.warning("autopilot.facts_refresh_failed sid=%s", sid, exc_info=True)
            fresh_obj = None
        if fresh_obj is None:
            return Decision(action="skip", reason="PR facts unavailable — refresh deferred",
                            waiting_reason="pr_facts_unavailable")
        from .completion_bar import PrFacts
        ctx.pr_facts = cast(PrFacts, fresh_obj)
        ctx.facts_verified = True
        return None

    async def _compose_nudge_decision(self, sid: str, ctx: DecisionContext,
                                      decision: Decision, now: float) -> Decision:
        """Compose the final nudge text (spec §3) and dedupe it (spec §4). No
        composer: a gate-authored prompt passes through (legacy); a composition-
        marked decision without text escalates — never a template."""
        from .autopilot_policy import near_duplicate
        if self._compose is None:
            if not (decision.prompt or "").strip():
                await self._record_futile(sid, ctx.ledger or {}, ctx)
                return Decision(action="escalate", reason="nudge needs composition — no composer")
            # A gate-authored prompt is held to the same anti-repetition rule as a
            # composed one: identical text for an unchanged state is never re-sent.
            if near_duplicate(decision.prompt or "", (ctx.ledger or {}).get("recent_nudges", []), now):
                await self._record_futile(sid, ctx.ledger or {}, ctx)
                return Decision(action="suppressed", reason="near-duplicate nudge suppressed")
            return decision
        try:
            text = await self._compose(ctx, decision)
        except HelperCredentialsPending:
            # Not a decline: nothing was attempted, so it is neither futile nor a
            # reason to hand the session to a human — the next tick retries (#2953).
            return Decision(action="skip", reason="helper credentials pending",
                            waiting_reason="credentials_pending")
        if not (text or "").strip():
            await self._record_futile(sid, ctx.ledger or {}, ctx)
            return Decision(action="escalate",
                            reason=f"composer declined/failed ({decision.reason})")
        assert text is not None  # narrowed: the empty/None check above would have returned
        if near_duplicate(text, (ctx.ledger or {}).get("recent_nudges", []), now):
            await self._record_futile(sid, ctx.ledger or {}, ctx)
            return Decision(action="suppressed", reason="near-duplicate nudge suppressed")
        return Decision(action="nudge", prompt=text, reason=decision.reason,
                        downgraded_from=decision.downgraded_from)

    async def _record_futile(self, sid: str, ledger: dict, ctx: DecisionContext) -> None:
        """A futile nudge attempt — either a near-duplicate or a composer decline/failure —
        counts toward loop escalation UNCONDITIONALLY so pre_decide's loop detection
        (repeat_count >= loop_repeat_limit) short-circuits the full pipeline (gate AND
        composer) after ~3 futile attempts.  For near-duplicates: repetition is keyed on
        what autopilot said, immune to the goal-text drift that resets `fingerprint`
        (spec §4).  For composer declines: prevents unbounded uncharged Sonnet calls on
        a persistently-declining session.

        ctx.ledger is updated in place so any subsequent write in the same tick (e.g.
        _escalate) inherits the incremented repeat_count and does not overwrite it."""
        new = dict(ledger)
        new.update(last_fingerprint=fingerprint(ctx),
                   repeat_count=ledger.get("repeat_count", 0) + 1)
        ctx.ledger = new
        await self._store(sid, new, ctx)

    async def _escalate(self, sid: str, ledger: dict, ctx: DecisionContext,
                        decision: Decision) -> None:
        # De-spam: a session already handed to a human for THIS stuck state stays
        # escalated silently — re-emitting "needs human" every tick buries the feed.
        # A changed fingerprint is new information and emits again.
        fp = fingerprint(ctx)
        await self._observe(sid, ledger, "handed_back" if ctx.status == "handed_back" else "escalated", ctx=ctx)
        ledger = ctx.ledger
        if ledger.get("escalated") and ledger.get("last_fingerprint") == fp:
            return
        new = dict(ledger)
        new.update(escalated=True, last_fingerprint=fp)
        await self._store(sid, new, ctx)
        reason = decision.reason
        if ctx.status != "handed_back":
            reason += await self._escalation_pr_note(sid, ctx)
        await self._ctx.emit("warn", f"{sid}: needs human — {reason}",
                             kind="escalated", session_id=sid)

    async def _escalation_pr_note(self, sid: str, ctx: DecisionContext) -> str:
        """Fresh PR facts for an escalation the human is about to read.

        Hard-rule escalations (loop, recovery cap) return before ``_verify_facts``,
        so they used to hand a human whatever the poller cache last said (#2928).
        Runs once per escalation episode (the dedupe above returns first), never
        for a hand-back — that turn is the user's and names its own ask. A
        failed refresh says so rather than presenting the cache as verified.
        """
        if self._refresh_facts is None or ctx.pr_facts is None or not ctx.pr_facts.has_repo:
            return ""
        from .completion_bar import PrFacts, met_bar
        from .fleet_context import pr_summary
        if not ctx.facts_verified:  # already fetched this tick by _verify_facts
            try:
                fresh = await self._refresh_facts(sid)
            except Exception:
                log.warning("autopilot.escalation_facts_refresh_failed sid=%s", sid,
                            exc_info=True)
                fresh = None
            if fresh is None:
                return " (PR facts could not be refreshed)"
            ctx.pr_facts = cast(PrFacts, fresh)
            ctx.facts_verified = True
        summary = pr_summary(ctx.pr_facts) or "no PR"
        verdict = met_bar(ctx.effective_goal, ctx.goal_source, ctx.pr_facts,
                          ctx.pr_resolution, instructions=ctx.instructions)
        likely = "; likely complete" if verdict.met and (
            ctx.pr_facts.merged_pr or ctx.pr_facts.armed_pr) else ""
        return f" (verified on GitHub: {summary}{likely})"
