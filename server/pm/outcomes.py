"""Dispatch outcome history — pure helpers (spec 2026-09-12 product automation
policy §6/§7). No loop logic: the writers live on ``AutomationContext``, the
readers (queue, roadmap, API) call these with their own session."""
from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select, tuple_
from sqlalchemy.sql import Select

from server.db.models import DISPATCH_OUTCOMES, DispatchOutcome, Project, WorkItem
from server.pm.refs import split_ref
from server.sessions.model_catalog import TIER_ORDER, model_tier

log = logging.getLogger(__name__)

# Outcomes that say nothing about how a dispatch went: ``running`` has not ended and
# ``superseded`` was replaced before it could answer. Everything else is evidence.
_UNFINISHED_OUTCOMES: tuple[str, ...] = ("running", "superseded")

# A bounce is a dispatch that ENDED without a merge: ``running`` has not ended,
# ``merged`` did not bounce, and ``superseded`` was replaced before it could answer
# — a double tap must not read as the issue being unworkable (R3-9). Derived from
# the vocabulary so a new outcome counts as a bounce unless it is named here.
FINALISED_BOUNCE_OUTCOMES: frozenset[str] = (
    frozenset(DISPATCH_OUTCOMES) - {"running", "merged", "superseded"})


def body_fingerprint(title: str | None, body: str | None) -> str:
    """THE "same issue body" identity for D8 — computed at spawn (written on the
    outcome row) and at rank/tree time (from the mirror row) through this one
    function, so an edited issue stops matching on both sides at once."""
    return hashlib.sha256(f"{title or ''}\n\n{body or ''}".encode()).hexdigest()


def bounced_out(bounces: int, give_up_after_bounces: int) -> bool:
    """THE bounce rule: the queue's ``needs_spec`` skip and the roadmap's
    ``needs_spec`` reason both call this. ``0`` means never."""
    return give_up_after_bounces > 0 and bounces >= give_up_after_bounces


def bounces_stmt(pairs: Iterable[tuple[str, str]]) -> Select:
    """Grouped counts of FINALISED non-merged outcomes for the given
    ``(target_ref, fingerprint)`` pairs. Callers execute it in their own
    session (sync or async) and hand the rows to ``rows_to_bounces``."""
    wanted = list(pairs)
    return (select(DispatchOutcome.target_ref, DispatchOutcome.body_fingerprint,
                   func.count(DispatchOutcome.id))
            .where(DispatchOutcome.outcome.in_(FINALISED_BOUNCE_OUTCOMES),
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
    subset that merged on the first pass. A row whose ``first_pass_merge`` is NULL
    is an outcome but not a first pass — unknown is never counted as success.
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
    ranked, and guessing a rank for them would put the floor on invented evidence.
    Emitted one entry per tier present, in ``TIER_ORDER``.
    """
    refs = [f"{repo}#{number}" for repo, number in (await db.execute(
        select(WorkItem.repo, WorkItem.number).where(
            WorkItem.feature_id == feature_id, WorkItem.kind == "issue")))
        if repo and number is not None]
    if not refs:
        return []
    rows = (await db.execute(
        select(DispatchOutcome.model, DispatchOutcome.first_pass_merge).where(
            DispatchOutcome.project_id == project_id,
            DispatchOutcome.target_ref.in_(refs),
            DispatchOutcome.outcome.not_in(_UNFINISHED_OUTCOMES)))).all()
    counts: dict[str, list[int]] = {}
    for model, first_pass in rows:
        tier = model_tier(model)
        if tier is None:
            continue
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
