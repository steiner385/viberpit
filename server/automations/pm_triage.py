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
  N+1. The auto-accept pass (spec §8): for a product whose policy opts in via
     ``triage_auto_accept`` (or Dispatch-off), accept its qualifying pending
     proposals through the SAME ``apply_accept`` the inbox uses — audited,
     undoable, capped at ``MAX_AUTO_ACCEPTS_PER_TICK`` per product per tick. The
     repeat rule (#2683) widens an opt-in, never replaces one. Propose-only stays
     the default: the mode vocabulary is unchanged and a product that never opts
     in never sees a machine accept. The same pass dismisses (no hold, undoable)
     proposals for closed items and bot artifacts.

Bot artifacts never reach step 1..N at all (#2683): after auto-linking, an item
its product's policy recognises as bot output is set aside through the
``mark_ignored`` seam instead of being classified.

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

from .context import observation_failed, publish_observation

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
from server.automations.product_policy import is_on
from server.pm.triage_accept import (
    DISMISS_REASON_BOT_ARTIFACT,
    DISMISS_REASON_ITEM_CLOSED,
    RULE_DISPATCH_OFF,
    RULE_REPEAT,
    auto_accept_eligible,
    is_bot_artifact,
    item_is_open,
    repeat_threshold_for,
)

from .base import AutomationStatus, ConfigField
from .engine_policy import EnginePolicy

log = logging.getLogger(__name__)

# The classify day-cap is a rolling 24h window (mirrors SdlcIntakeLoop).
_DAY_SECONDS = 24 * 60 * 60

_DEFAULT_MAX_SPAWNS_PER_DAY = 8

# Per product, per tick (spec §8). Deliberately a CONSTANT and not a
# ``ConfigField``: it is a blast-radius bound on an unattended writer, not an
# operator preference — the tick is 60 s, so a real backlog drains within
# minutes anyway, while a knob invites "just set it to 1000" the one time it
# matters most.
MAX_AUTO_ACCEPTS_PER_TICK = 25


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
    author: str | None = None        # bot-artifact recognition (#2683)
    product_slug: str | None = None  # the owning product, whose policy judges it


@dataclass(frozen=True)
class AutoAcceptCandidateView:
    """One pending proposal as the auto-accept step sees it (all I/O injected)."""
    proposal_id: str
    work_item_id: str
    product_slug: str | None       # None: the repo maps to no product → skip
    repo: str
    number: int
    confidence_level: str | None
    proposed_feature_id: str | None
    feature_state: str | None      # the target capability's lifecycle state (None if no row)
    auto_undone: bool | None
    title: str | None = None       # the work item's title (bot-artifact recognition)
    author: str | None = None      # the work item's author (bot-artifact recognition)
    item_state: str | None = None  # open | closed | merged — a closed item is dismissed
    # Earlier proposals onto ``proposed_feature_id`` a PERSON accepted as proposed
    # (the repeat rule, #2683); machine accepts never count.
    prior_human_accepts: int = 0


class PmTriage:
    id = "pm-triage"
    name = "PM Triage"
    work_class = "maintenance"
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
ReadPolicies = Callable[[], Awaitable[dict[str, dict]]]
ListAutoAcceptCandidates = Callable[[], Awaitable[list[AutoAcceptCandidateView]]]
# (proposal_id, rule) -> audit detail ({"workItemId", "featureId", "confidence",
# "rule"}) or None when the proposal was no longer pending (a human got there first).
AutoAccept = Callable[[str, str], Awaitable["dict | None"]]
# (proposal_id, reason) -> True when dismissed, False when no longer pending (#2683).
AutoDismiss = Callable[[str, str], Awaitable[bool]]
# item -> True when the bot artifact was taken out of the classify queue, False
# when it had already left it (#2683).
MarkIgnored = Callable[[PendingItemView], Awaitable[bool]]


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
        read_policies: ReadPolicies | None = None,
        list_auto_accept_candidates: ListAutoAcceptCandidates | None = None,
        auto_accept: AutoAccept | None = None,
        auto_dismiss: AutoDismiss | None = None,
        mark_ignored: MarkIgnored | None = None,
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
        # Optional (default None) so every existing construction — and every
        # test that predates the auto-accept pass — keeps working; the pass is
        # skipped outright unless all three are wired.
        self._read_policies = read_policies
        self._list_candidates = list_auto_accept_candidates
        self._auto_accept = auto_accept
        self._auto_dismiss = auto_dismiss
        self._mark_ignored = mark_ignored
        self._spawns_today: list[float] = []
        self._was_capped = False

    async def tick(self) -> None:
        try:
            await self._run()
        except Exception as exc:
            observation_failed(self._ctx)
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
            return

        self._observation_degraded = False
        # Read once per tick: the pre-classify bot-artifact skip and the
        # auto-accept pass judge each product by the same snapshot.
        policies = await self._policies_for_tick()
        pending = await self._list_pending()

        # 0. Mechanical auto-link pass (design §6.2): PRs whose closing refs point
        #    at a classified issue inherit that feature; an item carrying a label
        #    a feature doc routes (`labels:`) is homed the same way — no AI
        #    session either way. Linked/routed items leave the queue so only
        #    unresolved ones reach the classifier. The pass is per-item and
        #    independent, so it is guarded per item: an ordinary GitHub
        #    404/timeout on item 3 of 50 must not abandon the whole tick (no
        #    classify, no drift, no stale, no status refresh).
        still_pending: list[PendingItemView] = []
        for item in pending:
            if (item.kind == "pr" and item.linked_issue_numbers) or item.labels:
                try:
                    linked = await self._auto_link(item)
                except Exception as exc:
                    self._observation_degraded = True
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
        still_pending = await self._skip_bot_artifacts(still_pending, policies)

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

        # The engine picks ONE action per tick; the auto-accept pass is not one
        # of them — it is policy-driven bookkeeping over proposals that already
        # exist, so it runs on every propose-mode tick regardless of which
        # action fired (a classify-saturated backlog must not stall accepts).
        await self._auto_accept_pass(policies)

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
        if not session:
            # A falsy result means the seam spawned NOTHING this call — no
            # project owns the batch's repo, the engine policy refused the
            # launch, the fleet had no capacity, or (#2235) the batch's dedupe
            # key already matched a session an earlier tick spawned and is
            # still ingesting. None of those burn a spawn: counting them against
            # ``_spawns_today`` would exhaust the daily cap on ticks that
            # dispatched nothing, starving the ones that could.
            return
        self._spawns_today.append(self._clock())
        await self._ctx.emit(
            "info", f"classifying {len(batch)} items",
            kind="classify", session_id=getattr(session, "id", None),
            detail={"numbers": [i.number for i in batch]})

    async def _policies_for_tick(self) -> dict[str, dict] | None:
        """slug → validated product policy for this tick, or ``None`` when the
        seam is not wired or the read failed. A failure warns ONCE (it skips both
        the pre-classify bot-artifact skip and the auto-accept pass) — ``{}``
        would read as "no product opted in" and hide the outage."""
        if self._read_policies is None:
            return None
        try:
            return await self._read_policies()
        except Exception as exc:
            self._observation_degraded = True
            log.warning("pm_triage.policy_read_failed", exc_info=True)
            await self._ctx.emit("warn", f"auto-accept skipped: policy read failed: {exc}",
                                 kind="auto_accept_skipped")
            return None

    @staticmethod
    def _dismisses_bot_output(policy: dict | None, title: str | None,
                              author: str | None) -> bool:
        """Whether *policy* has bot-artifact dismissal on AND this item is bot
        output by the ONE verdict (``is_bot_artifact``) with the product's own
        patterns. Shared by the pre-classify skip and the dismiss pass."""
        return bool(policy and policy.get("triage_dismiss_bot_artifacts")) and is_bot_artifact(
            title, author, (policy or {}).get("triage_bot_artifact_patterns") or ())

    async def _skip_bot_artifacts(self, items: list[PendingItemView],
                                  policies: dict[str, dict] | None) -> list[PendingItemView]:
        """#2683: bot output never reaches a classify session. An item the
        owning product's policy recognises as a bot artifact is taken out of the
        classify queue through the injected ``mark_ignored`` seam instead (audited
        by the seam) — no proposal, no inbox card, no session spent on it.

        Dropped from THIS tick's batch even when the seam fails: classifying bot
        output is exactly the waste the rule exists to stop, and the next tick
        retries the mark. An unowned item (no product, no policy) is not judged."""
        if self._mark_ignored is None or not policies:
            return items
        kept: list[PendingItemView] = []
        for item in items:
            policy = policies.get(item.product_slug) if item.product_slug else None
            if not self._dismisses_bot_output(policy, item.title, item.author):
                kept.append(item)
                continue
            target = f"{item.repo}#{item.number}"
            try:
                ignored = await self._mark_ignored(item)
            except Exception as exc:
                self._observation_degraded = True
                log.warning("pm_triage.bot_artifact_ignore_failed", exc_info=True)
                await self._ctx.emit("warn", f"could not set aside bot artifact {target}: {exc}",
                                     kind="bot_artifact_ignore_failed", target=target)
                continue
            if ignored:
                await self._ctx.emit(
                    "info", f"set aside bot artifact {target} (not sent to classification)",
                    kind="bot_artifact_ignored", target=target,
                    detail={"product": item.product_slug})
        return kept

    async def _dismiss(self, cand: AutoAcceptCandidateView, reason: str, text: str) -> None:
        """One auto-dismissal through the injected seam (``apply_dismiss``),
        guarded per proposal like an accept."""
        if self._auto_dismiss is None:
            return
        target = f"{cand.repo}#{cand.number}"
        try:
            dismissed = await self._auto_dismiss(cand.proposal_id, reason)
        except Exception as exc:
            self._observation_degraded = True
            log.warning("pm_triage.auto_dismiss_failed", exc_info=True)
            await self._ctx.emit("warn", f"auto-dismiss failed for {target}: {exc}",
                                 kind="auto_dismiss_failed", target=target)
            return
        if dismissed:
            await self._ctx.emit(
                "info", text, kind="auto_dismissed", target=target,
                detail={"proposalId": cand.proposal_id, "product": cand.product_slug,
                        "reason": reason})

    async def _auto_accept_pass(self, policies: dict[str, dict] | None) -> None:
        """Spec §8: for every product whose policy opts in, accept the pending
        proposals one of the rules admits (``auto_accept_eligible`` names it:
        the ``triage_auto_accept`` confidence tier, Dispatch-off, or the repeat
        rule) — through the SAME ``apply_accept`` the inbox uses (injected as
        ``auto_accept``), audited by the seam with the rule, undoable for the same
        window. Runs every tick so a policy change catches up on the next one.

        Before any accept, two dismissals (#2683) through the SAME
        ``apply_dismiss`` the inbox's dismiss route uses — no hold, undoable:
        a proposal whose work item closed or merged (ALWAYS — a closed item has
        no decision left, so there is no policy key and no product needed), and
        a bot artifact for a product with ``triage_dismiss_bot_artifacts`` on.
        Neither touches a proposal a human already undid (``auto_undone``).

        Fail-safe by construction: either DB read failing (policies, candidates)
        → nothing this tick, with a warn, and the tick carries on to ``_stash``;
        one proposal's failure is logged and skipped, never the pass; a proposal
        a human resolved between the list and the write answers ``None``/False
        from the seam and is silently dropped. Accepts are capped at
        ``MAX_AUTO_ACCEPTS_PER_TICK`` per product, whatever the rule.
        """
        if (policies is None or self._list_candidates is None
                or self._auto_accept is None):
            return
        accepted_by_product: dict[str, int] = {}
        capped: set[str] = set()
        # The pass's OTHER DB read, guarded like the policy read (R2-7).
        # Unguarded, a transient failure here escapes ``_run`` and takes the
        # WHOLE tick with it — including the ``_stash`` that feeds the status
        # card — so a blip in this optional pass would blank counts the tick had
        # already computed. Skipping the pass costs one tick of auto-accepts;
        # the next tick re-reads and catches up.
        try:
            candidates = await self._list_candidates()
        except Exception as exc:
            self._observation_degraded = True
            log.warning("pm_triage.candidate_list_failed", exc_info=True)
            await self._ctx.emit("warn",
                                 f"auto-accept skipped: candidate read failed: {exc}",
                                 kind="auto_accept_skipped")
            return
        for cand in candidates:
            target = f"{cand.repo}#{cand.number}"
            if self._auto_dismiss is not None and not cand.auto_undone \
                    and not item_is_open(cand.item_state):
                await self._dismiss(cand, DISMISS_REASON_ITEM_CLOSED,
                                    f"auto-dismissed {target}: the item is {cand.item_state}")
                continue
            slug = cand.product_slug
            if not slug:
                continue
            policy = policies.get(slug) or {}
            if self._auto_dismiss is not None and not cand.auto_undone \
                    and self._dismisses_bot_output(policy, cand.title, cand.author):
                await self._dismiss(cand, DISMISS_REASON_BOT_ARTIFACT,
                                    f"auto-dismissed bot artifact {target}")
                continue
            rule = auto_accept_eligible(
                tier=policy.get("triage_auto_accept") or "off",
                confidence_level=cand.confidence_level,
                proposed_feature_id=cand.proposed_feature_id,
                feature_state=cand.feature_state, auto_undone=cand.auto_undone,
                # #2683: Dispatch-off is a per-product opt-in, like the tier.
                dispatch_off_accept=bool(policy.get("triage_auto_accept_dispatch_off"))
                and not is_on(policy),
                repeat_threshold=repeat_threshold_for(policy),
                prior_human_accepts=cand.prior_human_accepts)
            if rule is None:
                continue
            if accepted_by_product.get(slug, 0) >= MAX_AUTO_ACCEPTS_PER_TICK:
                capped.add(slug)
                continue
            try:
                detail = await self._auto_accept(cand.proposal_id, rule)
            except Exception as exc:
                self._observation_degraded = True
                log.warning("pm_triage.auto_accept_failed", exc_info=True)
                await self._ctx.emit("warn", f"auto-accept failed for {target}: {exc}",
                                     kind="auto_accept_failed", target=target)
                continue
            if detail is None:
                continue          # a human resolved it first
            accepted_by_product[slug] = accepted_by_product.get(slug, 0) + 1
            await self._ctx.emit(
                "info", f"auto-accepted {target} ({self._rule_text(rule, cand)})",
                kind="auto_accepted", target=target,
                detail={"proposalId": cand.proposal_id, "product": slug,
                        **detail, "rule": rule})
        for slug in sorted(capped):
            await self._ctx.emit(
                "info", f"auto-accept cap reached for {slug} this tick "
                        f"({MAX_AUTO_ACCEPTS_PER_TICK}); the rest wait",
                kind="auto_accept_capped", target=slug)

    @staticmethod
    def _rule_text(rule: str, cand: AutoAcceptCandidateView) -> str:
        """The feed's account of WHY the machine took a proposal."""
        if rule == RULE_REPEAT:
            return (f"repeat rule: {cand.prior_human_accepts} earlier human accepts "
                    f"onto this capability")
        if rule == RULE_DISPATCH_OFF:
            return f"Dispatch-off rule, {cand.confidence_level or 'low'} confidence"
        return f"confidence rule, {cand.confidence_level}"

    def _prune_day(self) -> None:
        cutoff = self._clock() - _DAY_SECONDS
        self._spawns_today = [t for t in self._spawns_today if t >= cutoff]

    def _stash(self, unclassified: int, pending: int, drift: int) -> None:
        publish_observation(self._ctx, "pm_triage_counts", {
            "unclassified": unclassified, "pending": pending, "drift": drift},
                            degraded=self._observation_degraded)
