"""`report_feedback` policy: validate, fingerprint, cap, and persist one report.

Pure policy plus ONE DB write. Never talks to GitHub — filing is the Phase 2
automation's job, on the primary daemon, so a worker host that runs the session only
needs the shared DB (session tools spec §4–§6).
"""
from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from server.mcp_spec import ToolArgError
from server.obs.metrics import session_feedback_total

log = logging.getLogger(__name__)

CATEGORIES: tuple[str, ...] = ("prompt", "dispatch", "orchestration", "environment", "tooling", "other")
IMPACTS: tuple[str, ...] = ("blocked", "degraded", "minor")
FEEDBACK_STATUSES: tuple[str, ...] = ("pending", "filed", "merged", "capped", "throttled", "dismissed")

_TITLE_MIN, _TITLE_MAX, _BODY_MAX, _EVIDENCE_MAX = 8, 120, 4000, 1000
_NORM = re.compile(r"[^a-z0-9]+")


def fingerprint(category: str, title: str) -> str:
    """Dedupe key: sha1 of category + normalized title, first 16 hex chars."""
    norm = _NORM.sub(" ", title.lower()).strip()
    return hashlib.sha1(f"{category} {norm}".encode()).hexdigest()[:16]


@dataclass(frozen=True)
class FeedbackItem:
    category: str
    impact: str
    title: str
    body: str
    evidence: str | None = None

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.category, self.title)


@dataclass(frozen=True)
class FeedbackResult:
    recorded: str | None   # row id, None only for "duplicate"
    status: str            # pending | capped | duplicate


def _text(args: Mapping[str, Any], key: str, *, required: bool, cap: int) -> str | None:
    raw = args.get(key)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        if required:
            raise ToolArgError(f"{key} is required")
        return None
    if not isinstance(raw, str):
        raise ToolArgError(f"{key} must be a string")
    return raw.strip()[:cap]


def validate_item(args: Mapping[str, Any]) -> FeedbackItem:
    category = str(args.get("category") or "").strip().lower()
    if category not in CATEGORIES:
        raise ToolArgError(f"category must be one of {', '.join(CATEGORIES)}")
    impact = str(args.get("impact") or "").strip().lower()
    if impact not in IMPACTS:
        raise ToolArgError(f"impact must be one of {', '.join(IMPACTS)}")
    title = _text(args, "title", required=True, cap=_TITLE_MAX) or ""
    if len(title) < _TITLE_MIN:
        raise ToolArgError(f"title must be at least {_TITLE_MIN} characters")
    body = _text(args, "body", required=True, cap=_BODY_MAX) or ""
    evidence = _text(args, "evidence", required=False, cap=_EVIDENCE_MAX)
    return FeedbackItem(category=category, impact=impact, title=title, body=body, evidence=evidence)


async def record_feedback(*, session_id: str, identity: str, item: FeedbackItem,
                          context: dict[str, Any], cap: int) -> FeedbackResult:
    """Persist one report. Idempotent per (session, fingerprint); durable cap by row count."""
    from server.audit import record_audit
    from server.db import db_session
    from server.db.models import SessionFeedback

    fp = item.fingerprint
    async with db_session() as db:
        dup = (await db.execute(select(SessionFeedback.id).where(
            SessionFeedback.session_id == session_id, SessionFeedback.fingerprint == fp))).first()
        if dup is not None:
            return FeedbackResult(recorded=None, status="duplicate")
        # Best-effort under concurrency: two interleaved calls can both read a count
        # below `cap` before either commits, so a session can end up with one more
        # "pending" row than `cap` strictly allows. Only dedupe is exact — the
        # (session_id, fingerprint) unique constraint below is the real guard against
        # a duplicate report, not this count.
        count = (await db.execute(select(func.count()).select_from(SessionFeedback).where(
            SessionFeedback.session_id == session_id,
            SessionFeedback.status != "capped"))).scalar_one()
        status = "pending" if count < cap else "capped"
        row = SessionFeedback(session_id=session_id, fingerprint=fp, category=item.category,
                              impact=item.impact, title=item.title, body=item.body,
                              evidence=item.evidence, context=dict(context), status=status)
        db.add(row)
        try:
            await db.flush()
        except IntegrityError:
            # Lost the race: another call for the same (session_id, fingerprint)
            # committed between our pre-check and this flush. The unique constraint
            # is the authority here, not the pre-check above.
            await db.rollback()  # clear the failed statement; exit commit is a no-op
            return FeedbackResult(recorded=None, status="duplicate")
        row_id = row.id
        try:
            await record_audit(db, "session.feedback", identity, {
                "session_id": session_id, "category": item.category, "impact": item.impact,
                "title": item.title, "fingerprint": fp, "status": status}, via="session")
        except Exception:  # noqa: BLE001 - an audit failure must never lose the report
            log.warning("session.feedback audit failed session=%s", session_id, exc_info=True)
        await db.commit()
    session_feedback_total.labels(item.category).inc()
    return FeedbackResult(recorded=row_id, status=status)
