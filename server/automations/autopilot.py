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
                 archive: Callable[[str], Awaitable[bool]] | None = None) -> None:
        self._ctx = ctx
        self._read_config = read_config
        self._list_governed = list_governed
        self._get_context = get_context
        self._apply = apply
        self._decide_fn = decide_fn
        self._clock = clock
        self._archive = archive          # goal met → archive the session (auto mode)
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
        ctx.ledger = ledger

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
        if decision.action == "skip":
            await self._record_fingerprint(sid, ledger, ctx)
            return False, False
        if decision.action == "complete":
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
            await self._escalate(sid, ledger, decision)
            return False, True

        if mode == "propose":
            await self._ctx.emit("info", f"{sid}: propose {decision.action} — {decision.reason}",
                                 kind="proposed", session_id=sid,
                                 detail={"action": decision.action, "reason": decision.reason})
            return False, False

        # Pass the DECIDE-TIME context to apply so it resolves exactly the
        # permission/question that pre_decide + post_validate just validated — never
        # a freshly re-read one (which could be a new, un-vetted pending permission
        # the session surfaced during the decider's await window).
        acted = await self._apply(sid, decision, ctx)
        if acted:
            self._day_actions.append(now)
            await self._charge(sid, ledger, ctx, decision, now)
            await self._ctx.emit("info", f"{sid}: {decision.action} — {decision.reason}",
                                 kind="acted", session_id=sid,
                                 detail={"action": decision.action})
        else:
            # The action did not mutate (e.g. a nudge that never landed). Still
            # accumulate the stuck-state fingerprint so loop detection can escalate
            # a session that would otherwise be nudged forever without progress.
            await self._record_fingerprint(sid, ledger, ctx)
        return acted, False

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
        new = dict(ledger)
        new.update(hour_actions=hour, last_fingerprint=fp, repeat_count=repeat,
                   recovery_attempts=recovery)
        await self._ctx.set_target_state(sid, new)

    async def _escalate(self, sid: str, ledger: dict, decision: Decision) -> None:
        new = dict(ledger)
        new["escalated"] = True
        await self._ctx.set_target_state(sid, new)
        await self._ctx.emit("warn", f"{sid}: needs human — {decision.reason}",
                             kind="escalated", session_id=sid)
