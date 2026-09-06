"""PM Triage — the continuous curation loop (design §6).

Same two-object mold as PmSync / SdlcIntake:

* ``PmTriage`` — the ``Automation`` contract surface (id/name/modes/config +
  status/on_state_change). Metadata only; holds no I/O. **Modes are
  ``("off", "propose")`` — there is NO ``auto`` in v1 (design §6, constitution).**
* ``PmTriageLoop`` — the supervised control loop. Every I/O dependency is
  INJECTED so the loop runs with no network / no real DB in tests. The loop
  never *decides* what to do: it delegates to the pure ``pm.engine`` and, for
  classify, REUSES phase-2 ``pm.classification`` via the injected ``classify``
  seam (the same code path as onboarding Step 2 — design §6.1, no second
  implementation).

A tick, in order (design §6):
  0. Auto-link the mechanical: every pending PR carrying closing-issue refs
     inherits that issue's feature (no session). Linked items leave the queue.
  1..N. Build the ``TriageState`` from DB counts and ask the engine for the ONE
     highest-priority action; execute it. Classify sessions are bounded by a
     rolling daily cap (like SdlcIntakeLoop).

**The engine is told the truth about budget and backlog.** ``count_state``
cannot see the loop's in-memory spawn ledger and its unclassified query can
outrun the auto-link pass, so the loop — not the seam — owns the numbers it
hands the engine (:meth:`PmTriageLoop._budgeted`): ``max`` of the two spawn
counts, ``min`` of the two caps, and ``unclassified_count`` clamped to the queue
that survived auto-linking. Every one of those is a *stricter-wins* composition,
which keeps all decisions in the engine: there is exactly one cap check and one
classify check, and both are the engine's.
"""
from __future__ import annotations

import logging
from contextlib import suppress
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable

from server.pm.engine import (
    ClassifyBatch,
    NeedsHuman,
    NudgeStale,
    ProposeFeatureMapDiff,
    TriageState,
    next_triage_action,
    qualifying_clusters,
)

from .base import AutomationStatus, ConfigField
from .engine_policy import EnginePolicy

log = logging.getLogger(__name__)

# The classify day-cap is a rolling 24h window (mirrors SdlcIntakeLoop).
_DAY_SECONDS = 24 * 60 * 60

_DEFAULT_MAX_SPAWNS_PER_DAY = 8


@dataclass(frozen=True)
class PendingItemView:
    """One open, unclassified work item with no pending proposal (the loop's
    ``list_pending`` seam filters to exactly these). ``linked_issue_numbers`` is
    non-empty only for PRs that reference an issue via a closing keyword."""
    repo: str
    number: int
    kind: str
    title: str
    body: str
    labels: tuple[str, ...]
    linked_issue_numbers: tuple[int, ...]


class PmTriage:
    id = "pm-triage"
    name = "PM Triage"
    description = (
        "Continuously curates the product model: classifies new backlog items "
        "into features, auto-links mechanical PRs, clusters map drift into a "
        "proposed feature-map diff, and nudges stale features. Propose-only — a "
        "human accept is the gate.")
    modes: tuple[str, ...] = ("off", "propose")
    # Audited engine ceiling (Phase 7, Task 7.1). A brand-new origin has no
    # Codex acceptance evidence, so it launches Claude only.
    engine_policy: EnginePolicy = EnginePolicy.CLAUDE_ONLY
    config_schema = [
        ConfigField("batch_size", "Items per classify session", "int", 50,
                    help="Open items classified in one session (design §6.1)."),
        ConfigField("max_spawns_per_day", "Max classify sessions / day", "int",
                    _DEFAULT_MAX_SPAWNS_PER_DAY,
                    help="Safety cap on classify sessions spawned per rolling 24h."),
        ConfigField("drift_threshold", "Drift cluster threshold", "int", 5,
                    help="Accepted no_fit items sharing a name before a feature-"
                         "map diff is proposed (design §6.3)."),
        ConfigField("stale_days", "Feature staleness window (days)", "int", 14,
                    help="A discovery/delivering feature idle this long gets a "
                         "low-key staleness indicator (design §6.4)."),
    ]

    def status(self, ctx: Any) -> AutomationStatus:
        counts = getattr(ctx, "pm_triage_counts", None) if ctx else None
        if not counts:
            return AutomationStatus(text="idle", level="idle")
        text = (f"{counts.get('unclassified', 0)} to triage · "
                f"{counts.get('pending', 0)} awaiting accept · "
                f"{counts.get('drift', 0)} drift")
        level = "active" if counts.get("unclassified") or counts.get("drift") else "idle"
        return AutomationStatus(text=text, level=level, counts=counts)

    async def on_state_change(self, *, enabled: bool, mode: str | None,
                              config: dict, ctx: Any) -> None:
        return None


ReadConfig = Callable[[], Awaitable[tuple[str, dict]]]
ListPending = Callable[[], Awaitable[list[PendingItemView]]]
AutoLink = Callable[[PendingItemView], Awaitable[bool]]
CountState = Callable[[], Awaitable[TriageState]]
Classify = Callable[[list[PendingItemView], int], Awaitable[Any]]
ProposeMapDiff = Callable[[str, tuple[int, ...], str], Awaitable[Any]]
MarkStale = Callable[[tuple[str, ...]], Awaitable[None]]


class PmTriageLoop:
    """One ``tick()`` auto-links mechanical PRs, then executes the single engine
    action for this tick. All I/O is injected."""

    def __init__(
        self,
        ctx: Any,
        *,
        read_config: ReadConfig,
        list_pending: ListPending,
        auto_link: AutoLink,
        count_state: CountState,
        classify: Classify,
        propose_map_diff: ProposeMapDiff,
        mark_stale: MarkStale,
        clock: Callable[[], float],
    ) -> None:
        self._ctx = ctx
        self._read_config = read_config
        self._list_pending = list_pending
        self._auto_link = auto_link
        self._count_state = count_state
        self._classify = classify
        self._propose_map_diff = propose_map_diff
        self._mark_stale = mark_stale
        self._clock = clock
        self._spawns_today: list[float] = []
        self._was_capped = False

    async def tick(self) -> None:
        try:
            await self._run()
        except Exception as exc:
            # A warn (not just a log line): every other event this loop emits is
            # ``info``, and the notification route fires on error/warn only, so a
            # silently-logged tick failure is invisible to an operator.
            log.warning("pm_triage.tick_error", exc_info=True)
            with suppress(Exception):  # tick() is the supervised boundary
                await self._ctx.emit("warn", f"pm-triage tick failed: {exc}",
                                     kind="tick_error")

    async def _run(self) -> None:
        mode, config = await self._read_config()
        if mode == "off":
            self._stash(0, 0, 0)
            return

        pending = await self._list_pending()

        # 0. Mechanical auto-link pass (design §6.2): PRs whose closing refs point
        #    at a classified issue inherit that feature with no AI session. Linked
        #    items leave the queue so only unlinked ones reach the classifier.
        #    The pass is per-item and independent, so it is guarded per item: an
        #    ordinary GitHub 404/timeout on item 3 of 50 must not abandon the
        #    whole tick (no classify, no drift, no stale, no status refresh).
        still_pending: list[PendingItemView] = []
        for item in pending:
            if item.kind == "pr" and item.linked_issue_numbers:
                try:
                    linked = await self._auto_link(item)
                except Exception as exc:
                    log.warning("pm_triage.auto_link_failed", exc_info=True)
                    await self._ctx.emit(
                        "warn", f"auto-link failed for {item.repo}#{item.number}: {exc}",
                        kind="auto_link_failed", target=f"{item.repo}#{item.number}")
                    linked = False
                if linked:
                    await self._ctx.emit(
                        "info", f"auto-linked {item.repo}#{item.number}",
                        kind="auto_linked", target=f"{item.repo}#{item.number}")
                    continue
            still_pending.append(item)

        self._prune_day()
        state = self._budgeted(await self._count_state(), config, still_pending)
        action = next_triage_action(state, config)

        capped = state.unclassified_count > 0 and state.day_spawns >= state.day_cap
        if capped and not self._was_capped:
            # Emitted on the TRANSITION into the capped state, not every capped
            # tick: the condition persists as long as the backlog does, and at a
            # 60 s cadence a re-emit would push every auto_linked/classify/
            # drift_proposed record off the 100-row feed within ~2 h. The
            # standing condition is what ``status()`` is for.
            await self._ctx.emit(
                "info", "daily classify-session cap reached; deferring",
                kind="ceiling")
        self._was_capped = capped

        if isinstance(action, ClassifyBatch):
            await self._classify_batch(still_pending, action.count)
        elif isinstance(action, ProposeFeatureMapDiff):
            await self._propose_map_diff(action.suggestion, action.item_numbers,
                                         action.repo)
            await self._ctx.emit(
                "info", f"proposed feature-map diff for '{action.suggestion}'",
                kind="drift_proposed", target=action.repo,
                detail={"suggestion": action.suggestion,
                        "items": list(action.item_numbers)})
        elif isinstance(action, NudgeStale):
            await self._mark_stale(action.slugs)
        elif isinstance(action, NeedsHuman):
            await self._ctx.emit("info", action.reason, kind="needs_human")
        # Idle: nothing.

        # The status card counts drift through the ENGINE's own rule — threshold
        # AND latch. Re-implementing the threshold half here would pin the card
        # at "N drift" forever, since ``no_fit`` clusters never shrink.
        self._stash(state.unclassified_count, state.pending_count,
                    len(qualifying_clusters(state, config)))

    def _budgeted(self, state: TriageState, config: dict,
                  still_pending: list[PendingItemView]) -> TriageState:
        """*state* with the classify budget and backlog the loop actually knows.

        ``count_state`` builds its view from the DB, which holds no record of the
        sessions THIS process spawned this window, so it answers ``day_spawns=0``
        forever. The loop keeps that ledger (``_spawns_today``), so it supplies
        it — otherwise the engine's ``day_spawns < day_cap`` check could never
        fail and the cap would have to be re-implemented in the dispatch path.

        Seam and ledger are two partial views of one budget, so the SAFE
        combination is ``max``: the seam's persisted count wins after a restart
        (a fresh process must not spend the day's budget twice), and the ledger
        wins when the seam lags. ``or`` would take the seam whenever it is
        non-zero and let a lagging count overrun the cap. Likewise the caps
        compose with a plain ``min`` — ``max_spawns_per_day`` is the operator's
        value and a seam ``day_cap=0`` is the STRICTEST budget, not "unset".

        ``unclassified_count`` is clamped to what is really classifiable after
        the auto-link pass. ``ClassifyBatch`` is the top of the engine's ladder
        and is gated on the count alone, so a count that outruns the queue (the
        auto-link write is not yet visible to ``count_state``'s query) makes the
        engine pick a batch the loop cannot fill — and the tick does nothing at
        all, starving drift and stale silently. Told the truth, the engine
        declines ``ClassifyBatch`` and falls through on its own.
        """
        cap = int(config.get("max_spawns_per_day", state.day_cap))
        return replace(
            state,
            unclassified_count=min(state.unclassified_count, len(still_pending)),
            day_spawns=max(state.day_spawns, len(self._spawns_today)),
            day_cap=min(cap, state.day_cap))

    async def _classify_batch(self, pending: list[PendingItemView], count: int) -> None:
        batch = pending[:count]
        if not batch:
            return
        session = await self._classify(batch, count)
        self._spawns_today.append(self._clock())
        await self._ctx.emit(
            "info", f"classifying {len(batch)} items",
            kind="classify", session_id=getattr(session, "id", None),
            detail={"numbers": [i.number for i in batch]})

    def _prune_day(self) -> None:
        cutoff = self._clock() - _DAY_SECONDS
        self._spawns_today = [t for t in self._spawns_today if t >= cutoff]

    def _stash(self, unclassified: int, pending: int, drift: int) -> None:
        self._ctx.pm_triage_counts = {
            "unclassified": unclassified, "pending": pending, "drift": drift}
