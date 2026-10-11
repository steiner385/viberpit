"""Dispatch outcome history — pure helpers (spec 2026-09-12 product automation
policy §6/§7). No loop logic: the writers live on ``AutomationContext``, the
readers (queue, roadmap, API) call these with their own session."""
from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, func, or_, select, tuple_
from sqlalchemy.sql import Select

from server.db.models import (
    DECISION_PROPOSED_CAUSE, DISPATCH_OUTCOMES, NOT_NEEDED_CAUSE, DispatchOutcome, Project, WorkItem)
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
    column and reads as ``implement`` (spec 2026-09-18 §3/§5). An ``investigate``
    dispatch (series item 4b) never opens a PR by design. Folded into
    :func:`_counts_as_attempt` below, which is what readers use."""
    return or_(DispatchOutcome.dispatch_kind.is_(None),
               DispatchOutcome.dispatch_kind == "implement")


def _counts_as_attempt():
    """THE "this row is evidence about implementing the issue" predicate — shared by
    every reader that counts attempts (``bounces_stmt``, ``last_attempt_model``,
    ``prior_attempts``, ``outcome_stats``), so a row cannot be a bounce in one and
    an invisible non-attempt in another.

    Two kinds of row are excluded, for the same reason in different columns:

    - an ``investigate`` dispatch (by KIND): it is not trying to open a PR;
    - a ``no_pr_needed`` row (#2660): the session found the objective already
      satisfied — a finding, not a failed attempt;
    - a ``decision_proposed`` row (by CAUSE, spec 2026-09-18 §6, review R1 F4): the
      session stopped to ask the owner a product question and parked the item on a
      ``decision`` hold. Counting it would charge the issue a bounce it did not
      earn — feeding the queue's ``needs_spec`` skip and the roadmap chip once the
      decision is accepted, so an item decided twice could be given up on for
      asking — and would spend a rung of the model ladder, since
      ``last_attempt_model`` would climb from the tier that merely asked.

    ``cause`` is nullable, and SQL's ``!=`` is not: ``cause != '…'`` is NULL (i.e.
    not true) for every row with no cause at all, which is nearly all of them. The
    IS NULL leg is what keeps those rows counted.
    """
    return and_(_implement_only(),
                or_(DispatchOutcome.cause.is_(None),
                    DispatchOutcome.cause.notin_((DECISION_PROPOSED_CAUSE,
                                                  NOT_NEEDED_CAUSE))))


def bounces_stmt(pairs: Iterable[tuple[str, str]]) -> Select:
    """Grouped counts of FINALISED non-merged ``implement`` outcomes for the given
    ``(target_ref, fingerprint)`` pairs. Callers execute it in their own
    session (sync or async) and hand the rows to ``rows_to_bounces``.

    Only rows that ``_counts_as_attempt`` admits: an ``investigate`` dispatch never
    opens a PR by design and a ``decision_proposed`` one stopped to ask the owner a
    question, so neither is a bounce (spec 2026-09-18 §3/§5/§6)."""
    wanted = list(pairs)
    return (select(DispatchOutcome.target_ref, DispatchOutcome.body_fingerprint,
                   func.count(DispatchOutcome.id))
            .where(DispatchOutcome.outcome.in_(FINALISED_BOUNCE_OUTCOMES),
                   _counts_as_attempt(),
                   tuple_(DispatchOutcome.target_ref,
                          DispatchOutcome.body_fingerprint).in_(wanted))
            .group_by(DispatchOutcome.target_ref, DispatchOutcome.body_fingerprint))


def rows_to_bounces(rows: Iterable[tuple[str, str, int]],
                    pairs: Iterable[tuple[str, str]]) -> dict[str, int]:
    """``{target_ref: bounces}`` for every pair, 0 when no row matched the
    CURRENT fingerprint (a row for an older body never counts). Named for
    ``bounces_stmt``, its usual caller, but the shape is generic — the never-ran
    cap's own count (:func:`never_ran_stmt`) reduces through it too."""
    want = dict(pairs)
    out = {ref: 0 for ref in want}
    for ref, fp, n in rows:
        if want.get(ref) == fp:
            out[ref] = int(n)
    return out


#: THE cause ``_release_never_ran`` (``backlog_runner.py``) writes for a dispatch
#: whose session never started turn one — spelled once here so the never-ran
#: cap's own count (:func:`never_ran_stmt`) and the writer can never drift apart.
NEVER_RAN_CAUSE = "session never ran a turn"


def never_ran_stmt(pairs: Iterable[tuple[str, str]]) -> Select:
    """Grouped counts of finalised ``abandoned``/:data:`NEVER_RAN_CAUSE` outcomes
    for the given ``(target_ref, fingerprint)`` pairs — the never-ran cap's own
    count (#1960 ladder default change), parallel to :func:`bounces_stmt` but for
    the no-penalty release the ordinary bounce dial never sees. Callers execute it
    in their own session and hand the rows to :func:`rows_to_bounces`, fingerprint
    scoping matching every other D8 count: an edited body re-arms it."""
    wanted = list(pairs)
    return (select(DispatchOutcome.target_ref, DispatchOutcome.body_fingerprint,
                   func.count(DispatchOutcome.id))
            .where(DispatchOutcome.outcome == "abandoned",
                   DispatchOutcome.cause == NEVER_RAN_CAUSE,
                   tuple_(DispatchOutcome.target_ref,
                          DispatchOutcome.body_fingerprint).in_(wanted))
            .group_by(DispatchOutcome.target_ref, DispatchOutcome.body_fingerprint))


# --------------------------------------------------------------------------- #
# Not-needed suppression (#2683)                                               #
# --------------------------------------------------------------------------- #
# A session that ends ``mark_no_pr("not-needed")`` found the objective already
# satisfied. Re-dispatching the SAME issue is pointless until something about it
# materially changes — and the runner used to do exactly that: cairnea/KinDash#17390
# ~45 times, #17391 ~10, viberpit#2924 within the hour. The verdict row (``no_pr`` /
# ``NOT_NEEDED_CAUSE``) records the issue's material identity, and the queue skips
# the issue as ``not_needed`` while it is unchanged, for a window that backs off
# with every verdict on the same material — so even an issue that never changes is
# looked at again only after a day, then 4, 16 and 30. A body/title edit, a label
# change, or a comment by a person after the verdict lifts it at once.
#
# Board Status is deliberately NOT part of the identity: the reconciler rewrites it
# from claims and holds (In progress while claimed, Held while held), so the mirror
# cannot tell a person's card move from its own stamp — every release would read as
# a change.

#: The suppression window after the 1st, 2nd, 3rd and every later not-needed
#: verdict on the same unchanged material. Never minutes (#2683).
NOT_NEEDED_BACKOFF: tuple[timedelta, ...] = (
    timedelta(days=1), timedelta(days=4), timedelta(days=16), timedelta(days=30))


def material_fingerprint(title: str | None, body: str | None,
                         labels: Iterable[str] | None) -> str:
    """THE "same issue, materially" identity for not-needed suppression: title, body
    and the (case-folded, sorted) label set. Written on the verdict row at finalise
    and computed from the mirror at rank time through this one function."""
    norm = sorted({str(lbl).strip().lower() for lbl in (labels or ()) if str(lbl).strip()})
    return hashlib.sha256(
        f"{title or ''}\n\n{body or ''}\n\nlabels:{','.join(norm)}".encode()).hexdigest()


def not_needed_backoff(verdicts: int) -> timedelta:
    """The window after the *verdicts*-th verdict on unchanged material (>= 1)."""
    return NOT_NEEDED_BACKOFF[min(max(verdicts, 1), len(NOT_NEEDED_BACKOFF)) - 1]


@dataclass(frozen=True)
class NotNeededVerdict:
    """One finalised ``no_pr`` / ``NOT_NEEDED_CAUSE`` row, as suppression reads it."""

    ended_at: datetime | None
    material_fingerprint: str | None
    body_fingerprint: str | None


def not_needed_stmt(refs: Iterable[str]) -> Select:
    """Every not-needed verdict for *refs*: ``(target_ref, ended_at,
    material_fingerprint, body_fingerprint)``. Implement dispatches only, like every
    other attempt read."""
    return (select(DispatchOutcome.target_ref, DispatchOutcome.ended_at,
                   DispatchOutcome.material_fingerprint, DispatchOutcome.body_fingerprint)
            .where(DispatchOutcome.outcome == "no_pr",
                   DispatchOutcome.cause == NOT_NEEDED_CAUSE,
                   _implement_only(),
                   DispatchOutcome.target_ref.in_(list(refs))))


def _utc(value: datetime | None) -> datetime | None:
    # SQLite hands back naive datetimes for timezone=True columns; every column is UTC.
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def not_needed_suppression(verdicts: Iterable[NotNeededVerdict], *, title: str | None,
                           body: str | None, labels: Iterable[str] | None,
                           last_human_comment_at: datetime | None,
                           ) -> tuple[datetime | None, bool]:
    """``(suppressed_until, standing)`` for an issue as it is NOW.

    *standing* — some not-needed verdict still matches the issue's current material
    (``material_fingerprint``; a row from before that column falls back to its
    ``body_fingerprint``) and no person has commented since the newest such verdict.
    *suppressed_until* — when a standing verdict's window ends: the newest matching
    verdict plus ``not_needed_backoff(<matching verdicts>)``; ``None`` when nothing
    stands. The window may already be in the past — the queue compares it with
    ``now``; the hold-hygiene pass reads *standing* alone."""
    material = material_fingerprint(title, body, labels)
    body_fp = body_fingerprint(title, body)
    ended = [at for v in verdicts
             if (at := _utc(v.ended_at)) is not None
             and (v.material_fingerprint == material if v.material_fingerprint
                  else v.body_fingerprint == body_fp)]
    if not ended:
        return None, False
    newest = max(ended)
    commented = _utc(last_human_comment_at)
    if commented is not None and commented > newest:
        return None, False
    return newest + not_needed_backoff(len(ended)), True


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
    do the work — and so is every row ``_counts_as_attempt()`` excludes (the same
    predicate ``bounces_stmt`` / ``last_attempt_model`` / ``prior_attempts`` apply):
    an ``investigate`` dispatch never opens a PR by design and a
    ``decision_proposed`` one asked the owner a question instead of trying, so each
    would land as a first-pass-less outcome at its tier and drag the floor down.
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
            _counts_as_attempt()))).all()
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


# --- The approval-gated hand-off: writer and reader as ONE pair -----------------
#
# When the ladder stops below the operator's configured top because the
# model-approval gate clamped it, the runner's owner_action hold says so
# ("bounced at opus; fable needs approval"). The Roadmap offers "Approve
# escalation to fable" on exactly those holds, so the sentence the runner WRITES
# and the structure the holds API READS back must be one definition — the same
# shape ``server/sessions/model_request.py`` uses for its prompt/scan halves.

#: The hand-off clause that names the gated tier. Appended to the runner's bounce
#: prefix ("bounced at <tier>" / "PR Doctor gave up at the top").
_NEEDS_APPROVAL = "; {tier} needs approval"
_NEEDS_APPROVAL_RE = re.compile(
    r"^(?:bounced at (?P<bounced>[a-z0-9\[\]\-\.]+)|.+?); (?P<tier>[a-z]+) needs approval$")


def needs_approval_suffix(gated: str) -> str:
    """The one spelling of "<tier> needs approval" the hand-off reason carries."""
    return _NEEDS_APPROVAL.format(tier=gated)


@dataclass(frozen=True)
class Escalation:
    """An owner_action hold a person can release by approving a climb: the tier
    the ladder bounced at (``None`` when the hand-off came from PR Doctor and
    names no bounce) and the gated tier that needs the approval."""
    bounced_at: str | None
    needs_approval: str


def escalation_for(hold_kind: str | None, hold_reason: str | None) -> Escalation | None:
    """Read the structured escalation back out of a hold, or ``None`` when the hold
    is not one the runner wrote at an approval-clamped top. Only an
    ``owner_action`` hold qualifies — a ``stronger_model`` park is already climbing
    and a ``decision``/``evidence`` hold ends another way — and the gated tier must
    be a real ladder tier (``TIER_ORDER``), so a hand-rolled reason that merely
    resembles the sentence cannot produce an approve button for a model the ladder
    has no rung for."""
    if hold_kind != "owner_action" or not hold_reason:
        return None
    m = _NEEDS_APPROVAL_RE.match(hold_reason.strip())
    if m is None or m.group("tier") not in TIER_ORDER:
        return None
    return Escalation(bounced_at=m.group("bounced"), needs_approval=m.group("tier"))


def escalation_to_wire(esc: Escalation | None) -> dict | None:
    """``{"bouncedAt", "needsApproval"}`` — the shape every route serialises it as."""
    if esc is None:
        return None
    return {"bouncedAt": esc.bounced_at, "needsApproval": esc.needs_approval}


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
    verdict and are skipped; ``_counts_as_attempt`` drops the rows that were not an
    attempt at all (investigate dispatches, and a session that proposed a decision —
    the ladder must not climb from a tier that merely asked a question)."""
    row = (await db.execute(
        select(DispatchOutcome.model).where(
            DispatchOutcome.target_ref == target_ref,
            DispatchOutcome.outcome.not_in(UNFINISHED_OUTCOMES),
            _counts_as_attempt())
        .order_by(DispatchOutcome.ended_at.desc().nullslast(), DispatchOutcome.created_at.desc())
        .limit(1))).first()
    return row[0] if row else None


async def prior_attempts(db, target_ref: str) -> int:
    """The ladder's ``attempt_no`` seed (spec 2026-09-18 §4, review R1 I3): the
    count of finalised, non-superseded ``implement`` dispatches already recorded
    for *target_ref*. Every bounce now deletes the runner's claim row (the
    ladder's hold, not the claim, carries the item forward), so the claim can no
    longer count its own attempts — a fresh claim reads this instead. Mirrors
    ``last_attempt_model``'s filter (``_counts_as_attempt()`` + excludes
    ``UNFINISHED_OUTCOMES``) so the two readers can never disagree about what
    counts as a past attempt. 0-based: an issue dispatched for the first time
    has 0 prior attempts, matching today's ``attempt_no`` semantics."""
    return int((await db.execute(
        select(func.count()).select_from(DispatchOutcome).where(
            DispatchOutcome.target_ref == target_ref,
            DispatchOutcome.outcome.not_in(UNFINISHED_OUTCOMES),
            _counts_as_attempt()))).scalar_one())
