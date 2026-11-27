"""Durable pending asks — ``session_holds`` (spec 002 FR-305 / FR-406, #947).

A pending permission, AskUserQuestion or plan approval is a live rendezvous between
an ``asyncio.Future`` and the SDK subprocess (see ``orphaned_asks``); a restart
destroys both, and until now also destroyed every trace of WHAT was being asked.
This module keeps a row per open ask so the next boot can report it lost with its
content (``provenance.HOLD_LOST``), and the card can show the question instead of
a bare "interrupted".

Writer contract: like ``provenance``, every DB helper here swallows and logs — a
hold is a record of the turn, never control flow, so it can never break the status
persist or the rehydrate that calls it. Failures log at INFO, not WARNING: a WARNING
auto-files an issue in this repo, and one DB outage during a hold transition would
otherwise file three (resolve, open, and the status persist's own warning — the
one that should name the condition). ``snapshot_content`` is pure.

The status→kind vocabulary is ``orphaned_asks.HOLD_KIND_BY_STATUS`` — one map for
the restart card and for the hold a status write opens.
"""
from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy import insert, select, update

from server.db import db_session
from server.db.clock import naive_utc_now
from server.db.models import SessionHold
from server.sessions import provenance
from server.sessions.orphaned_asks import HOLD_KIND_BY_STATUS
from server.time_iso import iso_utc

__all__ = [
    "HOLD_KIND_BY_STATUS", "MAX_CONTENT_BYTES", "MAX_STRING_CHARS",
    "hold_detail", "mark_lost", "newest_open", "open_hold", "resolve_open",
    "snapshot_content",
]

log = logging.getLogger(__name__)

#: Longest string value kept inside a snapshot (a Write's file content, a plan's
#: text). The lost-hold banner shows a summary; the full text is in the transcript.
MAX_STRING_CHARS = 4000
#: Ceiling on the serialized snapshot. Hit only by a card with many long fields;
#: bulky fields are shrunk progressively, identity keys (tool, permId, turnId) never.
MAX_CONTENT_BYTES = 16_384

#: The plan-approval card's tool (what ``derive_status`` reads as ``pending_plan``).
_PLAN_TOOL = "ExitPlanMode"


# --------------------------------------------------------------------------- snapshot

def _clip_strings(value: Any, limit: int) -> tuple[Any, bool]:
    """``value`` with every string cut to ``limit`` chars, plus whether any was cut."""
    if isinstance(value, str):
        return (value[:limit], True) if len(value) > limit else (value, False)
    if isinstance(value, dict):
        cut = False
        out: dict = {}
        for k, v in value.items():
            out[k], c = _clip_strings(v, limit)
            cut = cut or c
        return out, cut
    if isinstance(value, list):
        cut = False
        items = []
        for v in value:
            nv, c = _clip_strings(v, limit)
            items.append(nv)
            cut = cut or c
        return items, cut
    return value, False


def _size(value: dict) -> int:
    return len(json.dumps(value, default=str))


def _bounded(content: dict) -> dict:
    """``content`` under :data:`MAX_STRING_CHARS` / :data:`MAX_CONTENT_BYTES`, with
    ``truncated: True`` iff anything was cut."""
    clipped, truncated = _clip_strings(content, MAX_STRING_CHARS)
    for limit in (1000, 200):
        if _size(clipped) <= MAX_CONTENT_BYTES:
            break
        clipped, _ = _clip_strings(clipped, limit)
        truncated = True
    # Still too big (hundreds of fields): drop the bulky members, keep the identity.
    for key in ("items", "input", "questions"):
        if _size(clipped) <= MAX_CONTENT_BYTES:
            break
        if clipped.get(key):
            clipped[key] = [] if key == "questions" else None
            truncated = True
    if truncated:
        clipped["truncated"] = True
    # JSON-native once, here: a tool input can carry a non-JSON value (a Path, a
    # datetime); the JSON column's own encoder has no ``default`` and would fail every
    # open for that session. The size checks above used the same coercion.
    return json.loads(json.dumps(clipped, default=str))


def _pick_card(kind: str, permissions: list[dict]) -> dict | None:
    """The newest registry card for ``kind``: the plan card for a plan hold, the
    newest non-plan card for a permission hold; either falls back to the newest."""
    if not permissions:
        return None
    wanted = [p for p in permissions
              if (p.get("tool") == _PLAN_TOOL) == (kind == "plan")]
    return (wanted or permissions)[-1]


def snapshot_content(kind: str, permissions: list[dict], questions: list[dict]) -> dict:
    """The durable, identity-free picture of an open ask (pure).

    ``permissions`` is ``PermissionRegistry.pending(session_id)`` and ``questions``
    is ``driver.pending_questions()``. Only the card's content is copied — never the
    registry's ``owner`` (the resolving identity) or anything live. Wire shape is
    what ``web/src/api/types.ts`` ``LostHold.content`` reads.
    """
    if kind == "question":
        content: dict[str, Any] = {
            "questions": [{
                "questionId": q.get("question_id"),
                "header": q.get("header"),
                "question": q.get("question"),
                "options": q.get("options"),
                "multiSelect": q.get("multi_select"),
            } for q in questions],
            "turnId": questions[0].get("turn_id") if questions else None,
        }
    else:
        card = _pick_card(kind, permissions) or {}
        content = {
            "tool": card.get("tool"),
            "input": card.get("input"),
            "items": card.get("items"),
            "turnId": card.get("turn_id"),
            "permId": card.get("permId"),
        }
    return _bounded(content)


def hold_detail(hold: SessionHold) -> dict[str, Any]:
    """The ``hold_lost`` event detail and ``SessionRecord.lost_hold`` payload — one
    shape (``{kind, content, holdId, createdAt}``) so the card and the timeline agree."""
    return {"kind": hold.kind, "content": hold.content or {}, "holdId": hold.id,
            "createdAt": iso_utc(hold.created_at)}


# --------------------------------------------------------------------------- writers

async def open_hold(session_id: str, kind: str, content: dict | None) -> str | None:
    """Insert an open hold; returns its id, or None when the write failed (logged)."""
    hold_id = str(uuid.uuid4())      # the model's own id format (models._new_uuid)
    try:
        async with db_session() as db:
            await db.execute(insert(SessionHold).values(
                id=hold_id, session_id=session_id, kind=kind, content=content,
                created_at=naive_utc_now()))
    except Exception:
        log.info("holds.open_failed session=%s kind=%s", session_id, kind, exc_info=True)
        return None
    return hold_id


async def resolve_open(session_id: str) -> int:
    """Stamp ``resolved_at`` on every open hold of the session; returns how many."""
    try:
        async with db_session() as db:
            res = await db.execute(
                update(SessionHold)
                .where(SessionHold.session_id == session_id,
                       SessionHold.resolved_at.is_(None))
                .values(resolved_at=naive_utc_now()))
            return int(res.rowcount or 0)  # type: ignore[attr-defined]  # CursorResult at runtime
    except Exception:
        log.info("holds.resolve_failed session=%s", session_id, exc_info=True)
        return 0


async def newest_open(session_ids: Iterable[str]) -> dict[str, SessionHold]:
    """The newest open hold per session, in ONE query. Empty on failure (logged)."""
    ids = list(dict.fromkeys(session_ids))
    if not ids:
        return {}
    try:
        async with db_session() as db:
            rows = (await db.execute(
                select(SessionHold)
                .where(SessionHold.session_id.in_(ids),
                       SessionHold.resolved_at.is_(None))
                .order_by(SessionHold.created_at, SessionHold.id))).scalars().all()
    except Exception:
        log.info("holds.newest_open_failed n=%d", len(ids), exc_info=True)
        return {}
    # Ascending order: the last row seen per session is its newest.
    return {h.session_id: h for h in rows}


async def mark_lost(hold: SessionHold) -> None:
    """The restart killed this hold's waiter: record ``hold_lost`` with the snapshot,
    then close every open hold of the session. Event first — a duplicate event on a
    failed stamp beats a silently vanished ask."""
    await provenance.record(hold.session_id, provenance.HOLD_LOST, hold_detail(hold))
    await resolve_open(hold.session_id)
