"""Dispatch outcome history — pure helpers (spec 2026-09-12 product automation
policy §6/§7). No loop logic: the writers live on ``AutomationContext``, the
readers (queue, roadmap, API) call these with their own session."""
from __future__ import annotations

import hashlib
from collections.abc import Iterable
from typing import Any

from sqlalchemy import func, select, tuple_
from sqlalchemy.sql import Select

from server.db.models import DISPATCH_OUTCOMES, DispatchOutcome

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
