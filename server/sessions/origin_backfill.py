"""Reconstruct prompt provenance for nudges sent before the marker existed.

:mod:`server.sessions.prompt_origin` marks automated prompts going forward, but every
transcript already on disk was journalled bare — an autopilot nudge in one of those is
still indistinguishable from something the operator typed. This module recovers what it
honestly can from what autopilot happened to record at the time, and refuses to guess
beyond that.

Two sources, in order of confidence:

1. **Exact text** — ``AutomationTargetState.state["recent_nudges"]`` keeps the last
   ``RECENT_NUDGES_CAP`` nudge *texts* per session. An exact match is proof, not
   inference, so those entries are marked without the ``inferred`` flag.
2. **Timestamp** — ``AutomationEvent(kind="acted", detail.action="nudge")`` records
   *when* a nudge landed but not what it said. A prompt matched this way is marked
   ``inferred`` so the UI can show it as a reconstruction.

**Backfill is necessarily partial**, by two independent caps that predate this code:
``recent_nudges`` holds 5 entries per session and the row is deleted when the target
reaches terminal rest, and ``AutomationContext._prune`` keeps only 500 events per
automation *fleet-wide* — roughly a day or two of history for an automation as busy as
autopilot. Older transcripts stay unmarked. That is the honest outcome: a prompt this
cannot place is left as-is rather than guessed at.

The timestamp pass will not mark an ambiguous match. If more than one unmarked prompt
falls inside a nudge's window, every one of them is left alone — mislabelling something
the operator actually typed as "Autopilot" is a worse failure than leaving an automated
prompt unmarked, because it rewrites their own history at them.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from server.db import db_session
from server.db.models import AutomationEvent, AutomationTargetState
from server.sessions import events as ev

log = logging.getLogger("server.sessions.origin_backfill")

# The automation ids whose "acted/nudge" events are prompts into a session, mapped to
# the origin kind their prompts would carry today. Autopilot is the only one that
# records per-session nudge state; the completion-readiness nudge is orchestrator-owned
# and leaves no automation row, so it cannot be backfilled at all.
_NUDGE_AUTOMATIONS = {"autopilot": "autopilot"}

# How far apart a recorded nudge and its transcript entry may sit and still be
# considered the same prompt. The event row is written immediately after send_prompt
# returns, but the prompt itself can wait in the driver's FIFO queue behind a live turn
# before the CLI journals it — so the window is generous forward and tight backward.
_MATCH_BEFORE_S = 10.0
_MATCH_AFTER_S = 180.0


def _epoch_ms(dt: datetime) -> float:
    """Epoch ms from a column that is naive UTC on SQLite and aware on Postgres."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp() * 1000.0


async def _recorded_nudges(session_id: str) -> tuple[dict[str, str], list[tuple[float, str]]]:
    """Read both sources for *session_id*.

    Returns ``(texts, stamps)`` — exact nudge text → origin kind, and (epoch_ms, kind)
    for each recorded nudge action.
    """
    texts: dict[str, str] = {}
    stamps: list[tuple[float, str]] = []
    async with db_session() as db:
        rows = (await db.execute(
            select(AutomationTargetState)
            .where(AutomationTargetState.automation_id.in_(_NUDGE_AUTOMATIONS),
                   AutomationTargetState.target == session_id)
        )).scalars().all()
        for row in rows:
            kind = _NUDGE_AUTOMATIONS[row.automation_id]
            for entry in (row.state or {}).get("recent_nudges") or []:
                text = entry.get("text") if isinstance(entry, dict) else None
                if isinstance(text, str) and text.strip():
                    texts[text.strip()] = kind

        events = (await db.execute(
            select(AutomationEvent)
            .where(AutomationEvent.automation_id.in_(_NUDGE_AUTOMATIONS),
                   AutomationEvent.session_id == session_id,
                   AutomationEvent.kind == "acted")
        )).scalars().all()
        for e in events:
            detail: Any = e.detail or {}
            if isinstance(detail, dict) and detail.get("action") == "nudge" and e.ts:
                stamps.append((_epoch_ms(e.ts), _NUDGE_AUTOMATIONS[e.automation_id]))
    return texts, stamps


def _apply(events: list, texts: dict[str, str], stamps: list[tuple[float, str]]) -> int:
    """Mark unattributed user prompts in *events*. Returns how many were marked."""
    candidates = [e for e in events
                  if isinstance(e, ev.UserMessage) and e.origin is None]
    marked = 0

    # Pass 1 — exact text. Proof, so no `inferred` flag.
    for e in candidates:
        kind = texts.get((e.text or "").strip())
        if kind is not None:
            e.origin = {"kind": kind}
            marked += 1

    # Pass 2 — timestamp, over whatever pass 1 left. One nudge may only claim an
    # UNAMBIGUOUS entry: if its window holds two or more, it claims none of them.
    remaining = [(e, e.ts) for e in candidates if e.origin is None and e.ts is not None]
    for stamp_ms, kind in stamps:
        lo, hi = stamp_ms - _MATCH_BEFORE_S * 1000, stamp_ms + _MATCH_AFTER_S * 1000
        window = [(e, ts) for e, ts in remaining if lo <= ts <= hi]
        if len(window) != 1:
            continue
        hit = window[0]
        hit[0].origin = {"kind": kind, "inferred": True}
        remaining.remove(hit)
        marked += 1
    return marked


async def apply_origin_backfill(session_id: str, events: list) -> None:
    """Best-effort: tag pre-marker automated prompts in a replayed event list, in place.

    Called on the attach path once per replay window. Failure is swallowed — an
    un-backfilled transcript is the status quo, and a provenance nicety must never be
    the reason a session fails to open.
    """
    if not any(isinstance(e, ev.UserMessage) and e.origin is None for e in events):
        return
    try:
        texts, stamps = await _recorded_nudges(session_id)
        if not texts and not stamps:
            return
        marked = _apply(events, texts, stamps)
        if marked:
            log.debug("origin_backfill: marked %d prompt(s) sid=%s", marked, session_id)
    except Exception:  # pragma: no cover — best-effort provenance
        log.debug("origin_backfill failed sid=%s", session_id, exc_info=True)
