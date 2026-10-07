"""Whether a Backlog Runner claim's holder session is still alive (phantom claims).

A claim row names the session doing its work. Once that session is terminal —
``archived``/``ended``, ``complete`` (its driver is already released), or its row
is gone — nothing it does can still need the slot, yet the row keeps counting
until the runner's sweep settles it. When the sweep could not run (2026-10-06:
the runner's tick raised every minute for 14 h), eight such claims pinned
kindash at its ``max_in_flight`` of 8 and every KinDash session sat in the
session queue behind the product-capacity gate.

ONE definition of "dead holder", shared by the capacity readout's defect list,
every capacity count (``mark_dead_claims`` → ``backlog_runner.is_in_flight``)
and the settle-on-terminal paths, so a claim the governor calls a defect is
exactly a claim no cap counts.
"""
from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select

from server.db import db_session
from server.db.models import INACTIVE_SESSION_STATUSES, Session as DbSession

#: Session statuses under which a token-bearing claim can never be released by
#: the runner's sweep (D7): the terminal rows plus `complete`, which releases its
#: driver at once and was the #2009 shape. Deliberately narrower than
#: CAPACITY_EXEMPT_STATUSES — `queued` and `pending_approval` hold no runtime slot
#: yet but will start, so their claims are pending, not dead.
DEAD_CLAIM_STATUSES: frozenset[str] = INACTIVE_SESSION_STATUSES | {"complete"}

#: In-memory marker ``mark_dead_claims`` stamps on a COPY of a dead-holder row so
#: ``is_in_flight`` stops counting it. Never persisted: a stamped copy differs from
#: the stored row, so it can never pass a ``compare_target_state`` fence by accident.
DEAD_HOLDER_KEY = "_holder_dead"


def holder_session_id(state: dict) -> str | None:
    """The session a token-bearing, slot-holding claim is waiting on, else None.

    Only rows that occupy a slot can be phantom load: ``gave_up`` and
    ``handed_off`` rows already hold none, a ``proposed`` row has no session yet,
    and a token-less legacy row is ``reconcile_legacy_targets``' to discharge."""
    if (state.get("gave_up") or state.get("handed_off") or state.get("proposed")
            or not state.get("ownership_token")):
        return None
    sid = state.get("session_id")
    return sid if isinstance(sid, str) and sid else None


def is_dead_status(status: str | None) -> bool:
    """A holder whose row is missing (None) or in DEAD_CLAIM_STATUSES."""
    return status is None or status in DEAD_CLAIM_STATUSES


async def session_statuses(session_ids: Iterable[str]) -> dict[str, str]:
    """``{id: status}`` for the rows that exist; one batched read."""
    ids = sorted(set(session_ids))
    if not ids:
        return {}
    async with db_session() as db:
        rows = (await db.execute(
            select(DbSession.id, DbSession.status).where(DbSession.id.in_(ids)))).all()
    return {sid: status for sid, status in rows}


async def dead_claim_targets(states: dict) -> dict[str, tuple[str, str | None]]:
    """``{target: (session_id, status-or-None)}`` for every claim whose holder is dead."""
    holders = {target: sid for target, st in states.items()
               if (sid := holder_session_id(st)) is not None}
    statuses = await session_statuses(holders.values())
    return {target: (sid, statuses.get(sid)) for target, sid in holders.items()
            if is_dead_status(statuses.get(sid))}


async def mark_dead_claims(states: dict) -> dict:
    """*states* with every dead-holder row replaced by a stamped copy.

    For capacity ACCOUNTING only (the stamp makes ``is_in_flight`` False); the
    input dict and its rows are never mutated, and the result must never be
    written back. A failed status read returns *states* unchanged — counting a
    dead claim for one more read is the pre-existing behaviour, never worse."""
    try:
        dead = await dead_claim_targets(states)
    except Exception:  # noqa: BLE001 — accounting must not fail on a liveness read
        return states
    if not dead:
        return states
    return {target: ({**st, DEAD_HOLDER_KEY: True} if target in dead else st)
            for target, st in states.items()}
