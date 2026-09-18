"""Dispatch outcome history — pure helpers (spec 2026-09-12 product automation
policy §6/§7). No loop logic: the writers live on ``AutomationContext``, the
readers (queue, roadmap, API) call these with their own session."""
from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, or_, select, tuple_
from sqlalchemy.sql import Select

from server.db.models import DISPATCH_OUTCOMES, DispatchOutcome, Project, WorkItem
from server.pm.refs import split_ref
from server.sessions.model_catalog import TIER_ORDER, model_tier, tier_rank

log = logging.getLogger(__name__)

# THE unfinished set: outcomes that say nothing about how a dispatch went, because
# ``running`` has not ended and ``superseded`` was replaced before it could answer.
# Everything else is evidence. Both readers below derive from this one definition, so
# a new unfinished outcome cannot land in one and be missed by the other.
UNFINISHED_OUTCOMES: frozenset[str] = frozenset({"running", "superseded"})

# A bounce is a dispatch that ENDED without a merge — a double tap must not read as
# the issue being unworkable (R3-9). Derived from the vocabulary so a new outcome
# counts as a bounce unless it is unfinished or a merge.
FINALISED_BOUNCE_OUTCOMES: frozenset[str] = (
    frozenset(DISPATCH_OUTCOMES) - UNFINISHED_OUTCOMES - {"merged"})


def body_fingerprint(title: str | None, body: str | None) -> str:
    """THE "same issue body" identity for D8 — computed at spawn (written on the
    outcome row) and at rank/tree time (from the mirror row) through this one
    function, so an edited issue stops matching on both sides at once."""
    return hashlib.sha256(f"{title or ''}\n\n{body or ''}".encode()).hexdigest()


def bounced_out(bounces: int, give_up_after_bounces: int) -> bool:
    """THE bounce rule: the queue's ``needs_spec`` skip and the roadmap's
    ``needs_spec`` reason both call this. ``0`` means never."""
    return give_up_after_bounces > 0 and bounces >= give_up_after_bounces


def dispatch_kind_for_hold(hold_kind: str | None) -> str:
    """THE hold → dispatch-kind rule (spec 2026-09-18 §5): an ``evidence``-held
    primary is an auto-filed error report nobody has triaged, so the runner
    INVESTIGATES it; everything else is the implementation the runner has always
    dispatched.

    One function because the answer is needed twice per dispatch and the two must
    agree: the loop stamps it onto the claim intent at claim time (so a recovery
    settle, which has only the intent, still knows what the dispatch was for), and
    the spawn reads it to pick the seed prompt. Returns a ``DISPATCH_KINDS`` value.
    """
    return "investigate" if hold_kind == "evidence" else "implement"


def _implement_only():
    """THE ``implement``-dispatch predicate: NULL predates the ``dispatch_kind``
    column and reads as ``implement`` (spec 2026-09-18 §3/§5). Shared by
    ``bounces_stmt`` and ``last_attempt_model`` so an ``investigate`` dispatch
    (series item 4b, never opens a PR by design) cannot count as a bounce in one
    reader and as an attempt to climb the ladder from in the other."""
    return or_(DispatchOutcome.dispatch_kind.is_(None),
               DispatchOutcome.dispatch_kind == "implement")


def bounces_stmt(pairs: Iterable[tuple[str, str]]) -> Select:
    """Grouped counts of FINALISED non-merged ``implement`` outcomes for the given
    ``(target_ref, fingerprint)`` pairs. Callers execute it in their own
    session (sync or async) and hand the rows to ``rows_to_bounces``.

    An ``investigate`` dispatch (series item 4b) never opens a PR by design, so it
    must never count as a bounce; NULL predates the column and reads as
    ``implement`` (spec 2026-09-18 §3/§5)."""
    wanted = list(pairs)
    return (select(DispatchOutcome.target_ref, DispatchOutcome.body_fingerprint,
                   func.count(DispatchOutcome.id))
            .where(DispatchOutcome.outcome.in_(FINALISED_BOUNCE_OUTCOMES),
                   _implement_only(),
                   tuple_(DispatchOutcome.target_ref,
                          DispatchOutcome.body_fingerprint).in_(wanted))
            .group_by(DispatchOutcome.target_ref, DispatchOutcome.body_fingerprint))


def rows_to_bounces(rows: Iterable[tuple[str, str, int]],
                    pairs: Iterable[tuple[str, str]]) -> dict[str, int]:
    """``{target_ref: bounces}`` for every pair, 0 when no row matched the
    CURRENT fingerprint (a row for an older body never counts)."""
    want = dict(pairs)
    out = {ref: 0 for ref in want}
    for ref, fp, n in rows:
        if want.get(ref) == fp:
            out[ref] = int(n)
    return out


def list_outcomes_stmt(target_ref: str) -> Select:
    """Every dispatch this issue has had, newest first (the API's history read)."""
    return (select(DispatchOutcome).where(DispatchOutcome.target_ref == target_ref)
            .order_by(DispatchOutcome.created_at.desc()))


def _iso(value: Any) -> str | None:
    return value.isoformat() if value else None


def outcome_to_dict(r: DispatchOutcome) -> dict:
    """One outcome row as the web sees it (camelCase keys, ISO datetimes)."""
    return {"id": r.id, "targetRef": r.target_ref, "projectId": r.project_id,
            "sessionId": r.session_id, "model": r.model, "attemptNo": r.attempt_no,
            "outcome": r.outcome, "cause": r.cause, "prNumber": r.pr_number, "prUrl": r.pr_url,
            "firstPassMerge": r.first_pass_merge, "bodyFingerprint": r.body_fingerprint,
            "startedAt": _iso(r.started_at), "endedAt": _iso(r.ended_at),
            "durationS": r.duration_s, "createdAt": _iso(r.created_at)}


# --- Model-pick floor (D9, spec §7) ------------------------------------------


@dataclass(frozen=True)
class TierStats:
    """One capability's finished dispatch history at one model tier.

    ``outcomes`` counts finalised, non-superseded dispatches; ``first_pass`` is the
    subset that merged on the first pass. A row with ``outcome == "merged"`` and
    ``first_pass_merge is None`` is excluded from BOTH counts — unknown is neither
    evidence of success nor of failure. Every other finalised row keeps counting as
    an outcome, and only ``first_pass_merge is True`` counts as a first pass.
    """

    tier: str          # a member of TIER_ORDER
    outcomes: int
    first_pass: int


def tier_floor_from_stats(stats: Iterable[TierStats]) -> str | None:
    """THE D9 floor rule (pure): the lowest tier that merged first-pass in >= 2
    outcomes, when every cheaper tier with >= 2 outcomes merged first-pass in none
    of them — and at least one such cheaper tier exists. A tier with fewer than two
    outcomes is not evidence in either role, so it neither establishes that the
    cheap tiers fail nor becomes the floor itself.

    Ranked by the catalog's ``TIER_ORDER`` (constitution VI: the one tier ordering),
    never by the caller's input order and never by a string compare. ``None`` means
    "no evidence to raise the pick" — the floor only ever raises an ``auto``
    assessment, so no floor is the safe answer.
    """
    by_tier = {s.tier: s for s in stats}
    evidence_below = False
    for tier in TIER_ORDER:
        s = by_tier.get(tier)
        if s is None or s.outcomes < 2:
            continue
        if s.first_pass >= 2 and evidence_below:
            return tier
        if s.first_pass > 0:
            return None          # a cheaper tier does work — no floor
        evidence_below = True
    return None


async def outcome_stats(db, project_id: str, feature_id: str) -> list[TierStats]:
    """Per-tier finished dispatch evidence for one capability's issues.

    The join is by ref: the capability's mirrored issues give ``owner/repo#N``, and
    ``DispatchOutcome.target_ref`` carries the same string. Rows still ``running``
    or ``superseded`` are excluded (neither reached a verdict), as are rows whose
    model maps to no tier — ``default``/``best``/an unknown pinned id cannot be
    ranked, and guessing a rank for them would put the floor on invented evidence —
    and so are ``merged`` rows whose ``first_pass_merge`` is NULL: the merge
    happened but nothing knows whether it was first pass, and counting it as an
    outcome with no first pass would read a tier that DID merge as one that cannot
    do the work — and so are ``investigate`` dispatches (``_implement_only()``,
    the same predicate ``bounces_stmt`` / ``last_attempt_model`` / ``prior_attempts``
    apply): one never opens a PR by design, so every one of them would land as a
    first-pass-less outcome at its tier and drag the capability floor down.
    Emitted one entry per tier present, in ``TIER_ORDER``.
    """
    refs = [f"{repo}#{number}" for repo, number in (await db.execute(
        select(WorkItem.repo, WorkItem.number).where(
            WorkItem.feature_id == feature_id, WorkItem.kind == "issue")))
        if repo and number is not None]
    if not refs:
        return []
    rows = (await db.execute(
        select(DispatchOutcome.model, DispatchOutcome.first_pass_merge,
               DispatchOutcome.outcome).where(
            DispatchOutcome.project_id == project_id,
            DispatchOutcome.target_ref.in_(refs),
            DispatchOutcome.outcome.not_in(UNFINISHED_OUTCOMES),
            _implement_only()))).all()
    counts: dict[str, list[int]] = {}
    for model, first_pass, outcome in rows:
        tier = model_tier(model)
        if tier is None:
            continue
        if outcome == "merged" and first_pass is None:
            continue           # merged, first-pass status unknown — not evidence
        tally = counts.setdefault(tier, [0, 0])
        tally[0] += 1
        tally[1] += 1 if first_pass is True else 0
    return [TierStats(tier, counts[tier][0], counts[tier][1])
            for tier in TIER_ORDER if tier in counts]


async def tier_floor(db, project_id: str, feature_id: str) -> str | None:
    """The D9 floor for one capability, read off its own dispatch history."""
    return tier_floor_from_stats(await outcome_stats(db, project_id, feature_id))


async def project_id_for_slug(db, product_slug: str | None) -> str | None:
    """The project id behind a product slug, or None (no slug, or no such product).
    THE slug→project resolution for the outcome paths — both ``dispatch_outcomes``
    INSERT paths and the floor read — so they cannot disagree about what a product is.
    """
    if not product_slug:
        return None
    return (await db.execute(select(Project.id).where(
        Project.slug == product_slug))).scalar_one_or_none()


async def tier_floor_for_dispatch(db, product_slug: str | None,
                                  primary_ref: str) -> str | None:
    """The floor for the dispatch about to be spawned, from its primary issue.

    Resolves the product's project and the issue's capability, then defers to
    ``tier_floor``. Best-effort by §13: an unknown product, an unmirrored or
    unparseable ref, an issue on no capability, or a failed read all yield ``None``
    — a history lookup must never block a spawn.
    """
    try:
        project_id = await project_id_for_slug(db, product_slug)
        parts = split_ref(primary_ref)
        if project_id is None or parts is None:
            return None
        repo, number = parts
        feature_id = (await db.execute(select(WorkItem.feature_id).where(
            WorkItem.repo == repo, WorkItem.number == number,
            WorkItem.kind == "issue"))).scalars().first()
        if not feature_id:
            return None
        return await tier_floor(db, project_id, feature_id)
    except Exception:
        log.warning("pm.outcomes.tier_floor_for_dispatch_failed product=%s ref=%s",
                    product_slug, primary_ref, exc_info=True)
        return None


# --- The model ladder (spec 2026-09-18 §4, decision 2) -----------------------


def top_tier(policy: dict) -> str:
    """The strongest tier the ladder climbs to for this product (spec §4, decision 2)."""
    top = str((policy or {}).get("ladder_top") or "")
    return top if top in TIER_ORDER else TIER_ORDER[-1]


def clamp_ladder_top(configured: str, allowed: Callable[[str], bool]) -> str:
    """THE ladder-top clamp (spec 2026-09-18 §4, decision 2 amendment): the strongest
    tier at or below *configured* that *allowed* accepts.

    ``SessionOrchestrator.create_session`` gates every requested model through
    ``server/sessions/model_policy.py:resolve_model_policy``, and a gated tier parks
    the session as a driverless ``pending_approval`` row. A ladder rung on such a
    tier would therefore hold a runner slot behind a human and never run — the
    opposite of "no approval step for ladder dispatches". So the ladder treats the
    highest ALLOWED tier as its top and hands the issue to the owner there.

    Walks ``TIER_ORDER`` downward from *configured* (an unrecognised value reads as
    the strongest tier, mirroring ``top_tier``). With nothing allowed it returns the
    cheapest tier: the ladder then hands off after the first bounce, which is the
    safe direction — a stalled item beats an item parked for approval forever.
    """
    start = tier_rank(configured)
    if start is None:
        start = len(TIER_ORDER) - 1
    for tier in reversed(TIER_ORDER[:start + 1]):
        if allowed(tier):
            return tier
    return TIER_ORDER[0]


def gated_ladder_top(policy: dict) -> str | None:
    """The CONFIGURED ``ladder_top`` the approval gate clamped away, or ``None``.

    THE reader of the ``ladder_top_configured`` annotation ``clamp_ladder_top``'s
    caller leaves on the runner's in-memory policy snapshot (never persisted, never
    in ``PRODUCT_POLICY_SCHEMA``), so every hand-off reason that has to explain why
    the ladder stopped below the operator's setting says it the same way.
    """
    configured = str((policy or {}).get("ladder_top_configured") or "")
    return configured if configured and configured != top_tier(policy) else None


def next_rung(previous_model: str | None, *, floor: str | None, default_model: str | None,
              top: str) -> str | None:
    """THE ladder rule (spec 2026-09-18 §4): one tier above the previous attempt,
    never below the capability floor or a pinned product default, ``None`` when
    the climb would pass ``top`` — a bounce at the top is the owner's.

    An unresolvable previous model (``auto``, ``default``, ``best``, unknown) climbs
    from ``sonnet``, so a first bounce under ``auto`` lands on ``opus``."""
    start = tier_rank(previous_model)
    if start is None:
        start = TIER_ORDER.index("sonnet")
    candidate = start + 1
    for bound in (floor, default_model):
        r = tier_rank(bound)
        if r is not None and r > candidate:
            candidate = r
    top_rank = tier_rank(top)
    if top_rank is None or candidate > top_rank:
        return None
    return TIER_ORDER[candidate]


async def last_attempt_model(db, target_ref: str) -> str | None:
    """The model the newest finalised implement dispatch for *target_ref* ran on
    (the ladder climbs from here). ``superseded`` and ``running`` rows carry no
    verdict and are skipped; investigate rows are not attempts."""
    row = (await db.execute(
        select(DispatchOutcome.model).where(
            DispatchOutcome.target_ref == target_ref,
            DispatchOutcome.outcome.not_in(UNFINISHED_OUTCOMES),
            _implement_only())
        .order_by(DispatchOutcome.ended_at.desc().nullslast(), DispatchOutcome.created_at.desc())
        .limit(1))).first()
    return row[0] if row else None


async def prior_attempts(db, target_ref: str) -> int:
    """The ladder's ``attempt_no`` seed (spec 2026-09-18 §4, review R1 I3): the
    count of finalised, non-superseded ``implement`` dispatches already recorded
    for *target_ref*. Every bounce now deletes the runner's claim row (the
    ladder's hold, not the claim, carries the item forward), so the claim can no
    longer count its own attempts — a fresh claim reads this instead. Mirrors
    ``last_attempt_model``'s filter (``_implement_only()`` + excludes
    ``UNFINISHED_OUTCOMES``) so the two readers can never disagree about what
    counts as a past attempt. 0-based: an issue dispatched for the first time
    has 0 prior attempts, matching today's ``attempt_no`` semantics."""
    return int((await db.execute(
        select(func.count()).select_from(DispatchOutcome).where(
            DispatchOutcome.target_ref == target_ref,
            DispatchOutcome.outcome.not_in(UNFINISHED_OUTCOMES),
            _implement_only()))).scalar_one())
