"""Holds on work items — the ONE implementation of spec 2026-09-18 §3/§8.

A hold says why an item is parked and who parked it. Kinds live in
``server.db.models.HOLD_KINDS``; this module owns who may write which kind and
the set/clear operations every route and tool must go through.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from server.db.models import HOLD_KINDS, WorkItem
from server.pm.outcomes import body_fingerprint

#: Kinds a non-person (session, assistant, loopback caller) may set or clear.
AGENT_SETTABLE: frozenset[str] = frozenset({"evidence", "stronger_model"})

#: System actors: the runner and the automations. They ARE the ladder and the
#: reject flows, so they may write any kind — but they are never "human".
SYSTEM_ACTORS: frozenset[str] = frozenset({"runner", "migration"})
SYSTEM_PREFIXES: tuple[str, ...] = ("automation:",)
AGENT_PREFIXES: tuple[str, ...] = ("session:", "assistant:")
#: The synthetic identity handed to an unattributed loopback caller
#: (server/auth/identity.py:OPEN_LAN_IDENTITY). Spelled here as a literal so this
#: module stays free of the auth package.
OPEN_LAN_ACTOR = "lan@local"

ActorClass = Literal["person", "agent", "system"]


class HoldPermissionError(ValueError):
    """The actor may not set or clear this hold kind."""


def actor_class(actor: str) -> ActorClass:
    if actor in SYSTEM_ACTORS or actor.startswith(SYSTEM_PREFIXES):
        return "system"
    if actor == OPEN_LAN_ACTOR or actor.startswith(AGENT_PREFIXES):
        return "agent"
    return "person"


def may_set(actor: str, kind: str) -> bool:
    if kind not in HOLD_KINDS:
        return False
    return actor_class(actor) != "agent" or kind in AGENT_SETTABLE


def set_hold(wi: WorkItem, *, kind: str, reason: str, actor: str, now: datetime) -> None:
    if kind not in HOLD_KINDS:
        raise ValueError(f"hold_kind must be one of {HOLD_KINDS}")
    if not (reason or "").strip():
        raise ValueError("hold_reason is required")
    if not may_set(actor, kind):
        raise HoldPermissionError(f"{actor_class(actor)} actors may not set {kind!r}")
    # Overwriting a DIFFERENT kind releases the one already there, so the same gate
    # ``clear_hold`` applies must apply to the outgoing kind too. Without it an agent
    # setting ``evidence`` over a person's ``owner_action`` un-parks the item and the
    # runner dispatches work a person explicitly stopped (spec §8 "an actor may clear
    # only a kind it may set"). Re-setting the same kind is a refresh, not a release.
    if (wi.hold_kind is not None and wi.hold_kind != kind
            and not may_set(actor, wi.hold_kind)):
        raise HoldPermissionError(
            f"{actor_class(actor)} actors may not replace a {wi.hold_kind!r} hold")
    wi.hold_kind = kind
    wi.hold_reason = reason.strip()
    wi.hold_by = actor
    wi.hold_set_at = now
    wi.hold_fingerprint = body_fingerprint(wi.title, wi.body)


#: The one spelling of why a rejected classification parks its item, shared by the
#: reject route and the undo snapshot readers.
REJECT_HOLD_REASON = "classification proposal rejected"


def set_reject_hold(wi: WorkItem, *, actor: str, now: datetime) -> None:
    """Park ``wi`` as ``owner_action`` because a classification proposal was rejected.

    Writes exactly the fields ``set_hold(kind="owner_action", …)`` writes but does NOT
    gate on the actor's class, because spec §8 authorises ``owner_action`` from "the
    two reject flows" whoever drives them: the rejection itself is the authority, not
    the caller. Gating here would 422 the inbox Reject button in LAN-open mode
    (``lan@local`` is agent class) and make an assistant rejection unrecordable — and
    the alternative, passing a person's login as the actor, is the provenance lie §8
    exists to remove. ``actor`` is therefore recorded as-is, truthfully.
    """
    wi.hold_kind = "owner_action"
    wi.hold_reason = REJECT_HOLD_REASON
    wi.hold_by = actor
    wi.hold_set_at = now
    wi.hold_fingerprint = body_fingerprint(wi.title, wi.body)


def clear_hold(wi: WorkItem, *, actor: str) -> None:
    if wi.hold_kind is None:
        return
    if not may_set(actor, wi.hold_kind):
        raise HoldPermissionError(
            f"{actor_class(actor)} actors may not clear a {wi.hold_kind!r} hold")
    wi.hold_kind = None
    wi.hold_reason = None
    wi.hold_by = None
    wi.hold_set_at = None
    wi.hold_fingerprint = None


def actor_for_request(request: object) -> str:
    """The actor string for an HTTP caller (spec §8): the drawer's bound token is
    ``assistant:<identity>``; otherwise the verified identity the middleware attached,
    which is ``lan@local`` for an unattributed loopback caller and a Tailscale login
    for a person. Takes any object with ``.state.identity`` / ``.state.via`` so the
    pure test needs no FastAPI Request."""
    state = getattr(request, "state", None)
    identity = str(getattr(state, "identity", "") or OPEN_LAN_ACTOR)
    if getattr(state, "via", None) == "assistant":
        return f"assistant:{identity}"
    return identity


async def apply_session_hold(db, *, ref: str, kind: str | None, reason: str,
                             session_id: str, now: datetime) -> tuple[WorkItem, str | None]:
    """The session tool's one call: resolve ``owner/repo#N`` to the mirrored issue and
    set (``kind``) or clear (``kind is None``) its hold as ``session:<id>``.

    Returns ``(work_item, released)`` where ``released`` is the hold kind that WAS on
    the item before a clear (``None`` when setting, and ``None`` when clearing an
    already-unheld item) — mirroring the overlay route's ``released = wi.hold_kind``
    capture (``server/api/pm.py``'s ``clearHold`` branch), since ``clear_hold`` blanks
    ``wi.hold_kind`` before a caller could otherwise read what was released."""
    from sqlalchemy import select

    from server.pm.refs import split_ref
    parts = split_ref(ref)
    if parts is None:
        raise ValueError(f"ref must look like owner/repo#123, got {ref!r}")
    repo, number = parts
    wi = (await db.execute(select(WorkItem).where(
        WorkItem.repo == repo, WorkItem.number == number,
        WorkItem.kind == "issue"))).scalar_one_or_none()
    if wi is None:
        raise ValueError(f"{ref} is not a mirrored issue")
    actor = f"session:{session_id}"
    released: str | None = None
    if kind is None:
        released = wi.hold_kind
        clear_hold(wi, actor=actor)
    else:
        set_hold(wi, kind=kind, reason=reason, actor=actor, now=now)
    await db.flush()
    return wi, released


#: THE name of the index backing every hold lookup, declared once here because two
#: writers need it: migration 0049 (the reviewable record) and
#: ``scripts/convert_legacy_needs_human.py`` (what actually runs on the live DB).
#: ``WorkItem.hold_kind`` carries ``index=True``, but ``create_all`` never touches an
#: existing table and ``init_db``'s reconcile ALTER-ADDs COLUMNS only — so without the
#: script the index would exist only on databases created from scratch. Both dialects
#: accept ``IF NOT EXISTS``, so the statement is safe to re-run.
HOLD_INDEX_NAME = "ix_work_items_hold_kind"
HOLD_INDEX_SQL = f"CREATE INDEX IF NOT EXISTS {HOLD_INDEX_NAME} ON work_items (hold_kind)"

#: Dialect-neutral conversion of pre-ladder ``needs_human`` rows (spec §3). Run by
#: migration 0049's ``upgrade()`` AND by ``scripts/convert_legacy_needs_human.py``,
#: because nothing runs ``alembic upgrade`` at deploy time — the script is what
#: actually converts the live rows. Order matters: the hold is written from the row
#: while ``triage_state`` still says needs_human, then the state is rewritten.
LEGACY_CONVERSION_SQL: tuple[str, ...] = (
    "UPDATE work_items SET hold_kind = 'owner_action', hold_by = 'migration', "
    "hold_reason = 'pre-ladder needs_human', hold_set_at = classified_at "
    "WHERE triage_state = 'needs_human' AND hold_kind IS NULL",
    "UPDATE work_items SET triage_state = 'classified' WHERE triage_state = 'needs_human' "
    "AND reach IS NOT NULL AND impact IS NOT NULL AND effort IS NOT NULL AND confidence IS NOT NULL",
    "UPDATE work_items SET triage_state = 'unclassified' WHERE triage_state = 'needs_human'",
)
