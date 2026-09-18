"""Holds on work items — the ONE implementation of spec 2026-09-18 §3/§8.

A hold says why an item is parked and who parked it. Kinds live in
``server.db.models.HOLD_KINDS``; this module owns who may write which kind and
the set/clear operations every route and tool must go through.
"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Literal

from server.db.models import HOLD_KINDS, WorkItem
from server.pm.outcomes import body_fingerprint

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.orm import Session

    from server.pm.mirror_store import HumanEdit

#: Kinds a non-person (session, assistant, loopback caller) may set or clear.
AGENT_SETTABLE: frozenset[str] = frozenset({"evidence", "stronger_model"})

#: System actors: the runner and the automations. They ARE the ladder and the
#: reject flows, so they may write any kind — but they are never "human".
#: (RUNNER_ACTOR / RECONCILER_ACTOR / SYSTEM_ACTORS are defined below, after
#: ``set_reject_hold``, next to the release rule that is their main consumer.)
SYSTEM_PREFIXES: tuple[str, ...] = ("automation:",)
#: The two agent prefixes, named because callers need to tell them APART as well as
#: from a person: the assistant acts on a person's instruction at their keyboard,
#: a session acts on its own (``server/pm/decisions.py:may_resolve``).
SESSION_PREFIX = "session:"
ASSISTANT_PREFIX = "assistant:"
AGENT_PREFIXES: tuple[str, ...] = (SESSION_PREFIX, ASSISTANT_PREFIX)
#: The synthetic identity handed to an unattributed loopback caller
#: (server/auth/identity.py:OPEN_LAN_IDENTITY). Spelled here as a literal so this
#: module stays free of the auth package.
OPEN_LAN_ACTOR = "lan@local"

ActorClass = Literal["person", "agent", "system"]


class HoldPermissionError(ValueError):
    """The actor may not set or clear this hold kind."""


class DecisionHoldError(ValueError):
    """A ``decision`` hold was handed to a release path. Spec §8 resolves one through
    its proposal, so both API taps map this to 422 ``bad_hold``."""


#: The one wording of that refusal, so the two routes cannot drift apart.
DECISION_HOLD_MESSAGE = "a decision hold is resolved through its proposal, not released"

#: The SET-side sibling (spec §8, "decision holds only via the decisions endpoint").
#: A ``decision`` hold exists only as the shadow of a pending proposal, so the one
#: writer is ``decisions.propose`` → :func:`set_decision_hold`.
DECISION_SET_MESSAGE = "decision holds are set only by a decision proposal"


def refuse_decision_release(wi: WorkItem) -> None:
    """Raise ``DecisionHoldError`` when *wi* carries a ``decision`` hold — THE
    guard every generic release path shares (:func:`release_via_api` for Retry and
    Implement now, the overlay PATCH's ``clearHold``). One function rather than the
    same two lines in each, so a third tap cannot quietly omit it: releasing a
    decision hold outside ``decisions.accept`` drops the question on the floor and
    re-queues the item with the decision still unmade."""
    if wi.hold_kind == "decision":
        raise DecisionHoldError(DECISION_HOLD_MESSAGE)


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
    # Refused for EVERY actor class, ahead of the class gate: a ``decision`` hold
    # is the shadow of a pending proposal (spec §8), and one set here — by the
    # overlay PATCH, by the ``set_hold`` session tool — would park the item with
    # nothing to resolve it, while every release path refuses the kind. Only
    # ``decisions.propose`` (via ``set_decision_hold``) may write one.
    if kind == "decision":
        raise DecisionHoldError(DECISION_SET_MESSAGE)
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


#: The one spelling of why a REJECTED decision proposal parks its item — the
#: ``owner_action`` sibling of ``REJECT_HOLD_REASON`` (spec 2026-09-18 §6). The
#: reject route, the undo readers and the tests all read it from here.
DECISION_REJECT_REASON = "decision proposal rejected"


def set_decision_hold(wi: WorkItem, *, reason: str, actor: str, now: datetime) -> None:
    """Park ``wi`` as ``decision`` because a proposal is pending (spec §6).

    Writes exactly the fields ``set_hold(kind="decision", …)`` writes but, like
    ``set_reject_hold``, does NOT gate on the actor's class: the proposal IS the
    authority, and the intended writer — a coding session (``session:<id>``,
    agent class) that hit a genuine product decision — could not otherwise set a
    kind outside ``AGENT_SETTABLE``. Recording a person's login instead would be
    the provenance lie §8 exists to remove, so ``actor`` is recorded as-is.

    The one thing this must never do is un-park an item a PERSON parked. That
    guard lives in the caller — ``decisions.propose`` refuses when the item
    already carries a ``HELD_KINDS`` hold set by a person — because only there is
    the refusal reportable to the proposer as a ``DecisionError``, and only there
    is the proposal row suppressed along with the hold.

    Not called directly by routes or tools: ``decisions.propose`` is the ONE
    writer of a ``decision`` hold (spec §8).
    """
    if not (reason or "").strip():
        raise ValueError("hold_reason is required")
    wi.hold_kind = "decision"
    wi.hold_reason = reason.strip()
    wi.hold_by = actor
    wi.hold_set_at = now
    wi.hold_fingerprint = body_fingerprint(wi.title, wi.body)


def clear_decision_hold(wi: WorkItem, *, actor: str) -> bool:
    """THE only release of a ``decision`` hold (spec §6/§8): accepting the proposal.

    Ungated for the same reason ``set_decision_hold`` is — an accept driven
    through the assistant or an unattributed loopback caller is agent class, and
    ``clear_hold`` would refuse it — but narrowed to the kind it owns: a
    ``decision`` hold only. Anything else on the row (a person's ``owner_action``
    that replaced this one, say) is somebody else's park and is left alone.

    ``actor`` is taken for call-shape symmetry with ``clear_hold`` and because it
    is what the caller audits; the class gate it would drive is deliberately
    absent (see above). Returns whether a hold was actually released, so the
    caller audits ``pm.hold.release`` only when one was.
    """
    if wi.hold_kind != "decision":
        return False
    wi.hold_kind = None
    wi.hold_reason = None
    wi.hold_by = None
    wi.hold_set_at = None
    wi.hold_fingerprint = None
    return True


def set_reject_decision_hold(wi: WorkItem, *, actor: str, now: datetime) -> None:
    """Park ``wi`` as ``owner_action`` because a decision proposal was REJECTED
    (spec §6): the options were wrong, so the item goes back to the person with
    ``DECISION_REJECT_REASON``. The ``decision`` hold it replaces is this call's
    own — the proposal being rejected — so there is nothing of anyone else's to
    protect, and like ``set_reject_hold`` the rejection is the authority, not the
    caller's class."""
    wi.hold_kind = "owner_action"
    wi.hold_reason = DECISION_REJECT_REASON
    wi.hold_by = actor
    wi.hold_set_at = now
    wi.hold_fingerprint = body_fingerprint(wi.title, wi.body)


RUNNER_ACTOR = "runner"
RECONCILER_ACTOR = "reconciler"
SYSTEM_ACTORS: frozenset[str] = frozenset({RUNNER_ACTOR, RECONCILER_ACTOR, "migration"})

#: Every way a hold ends (spec §7), as ONE tuple so the two API paths, the reconcile
#: pass and the runner cannot audit a release in different shapes. Time is not one
#: of them. ``board_edit`` / ``body_edit`` are detected by ``release_by_signals``;
#: ``retry`` is the Retry endpoint; ``overlay`` is an explicit clear through the
#: overlay editor (e.g. an agent taking the convert-to-fix exit on its own evidence
#: hold), which is an overlay EDIT as well as a release and so also stamps
#: ``classified_by``; ``session`` is a coding session clearing its own hold through
#: the ``set_hold`` tool (``server/sessions/tools/tools.py``, ``via="session"``);
#: ``implement`` is Implement now from the roadmap dialog: released and dispatched
#: in one tap; and ``dispatch`` is the one signal that ends no ``owner_action`` hold
#: at all — the Backlog Runner's spawn CONSUMING the ``stronger_model`` hold it
#: climbed for (``release_on_dispatch``, spec §4). It belongs in this tuple because
#: it audits through the same ``pm.hold.release`` shape, and a reader asking "how
#: can a hold end?" must find every answer in one place. ``investigation`` is the
#: Backlog Runner settling an ended INVESTIGATION on its convert-to-a-fix exit (spec
#: §5): the session found the root cause, so the ``evidence`` hold that made the
#: report an investigation instead of an implementation is spent. Finally, ``decision``
#: is the accept of a decision proposal (``server/pm/decisions.py:accept``) — the ONE
#: release of a ``decision`` hold, which is why ``release_via_api`` refuses that
#: kind outright: it does not end on a generic tap, it ends when the person picks
#: an option (spec 2026-09-18 §6).
RELEASE_SIGNALS: tuple[str, ...] = ("board_edit", "body_edit", "retry", "overlay", "session",
                                    "implement", "dispatch", "investigation", "decision")
#: Board Status values a human moves a card to in order to hand it back.
RELEASING_BOARD_STATUSES: frozenset[str] = frozenset({"Ready", "Backlog"})

Released = tuple[WorkItem, str]


def set_runner_hold(wi: WorkItem, *, reason: str, now: datetime) -> None:
    """The Backlog Runner's give-up (spec §3): the hold IS the handoff; the runner's
    own ``gave_up`` row survives only for slot accounting."""
    set_hold(wi, kind="owner_action", reason=reason, actor=RUNNER_ACTOR, now=now)


def set_runner_stronger_hold(wi: WorkItem, *, reason: str, now: datetime) -> None:
    """The Backlog Runner's ladder climb (spec 2026-09-18 §4): a bounce below the
    top tier parks the item as ``stronger_model`` — agent-settable, queue-eligible —
    so the next refill re-claims it and the spawn pins the next rung."""
    set_hold(wi, kind="stronger_model", reason=reason, actor=RUNNER_ACTOR, now=now)


#: The error-triage automation's actor. System class via ``SYSTEM_PREFIXES``, so the
#: hold it writes is attributed to the machine that filed the issue and never to a
#: person — spelled once here because the mirror writes it and every reader of a
#: birth hold (inbox, evidence dispatch) compares against it.
ERROR_TRIAGE_ACTOR = "automation:error-triage"

#: The one wording of why an auto-filed error report is born parked (spec §5). An
#: automation's stack trace is a symptom, not a diagnosis: the report must be
#: investigated before anyone implements against it, so the row enters the mirror
#: already holding rather than falling into the queue as ordinary work.
EVIDENCE_BIRTH_REASON = "auto-filed error report: investigate before implementing"


def set_evidence_hold(wi: WorkItem, *, reason: str, actor: str, now: datetime) -> None:
    """Park ``wi`` pending evidence, as ``actor``. Named — and taking ``actor`` rather
    than pinning one the way ``set_runner_hold`` does — because ``evidence`` is the one
    kind every class of actor writes: the mirror's birth hold (``ERROR_TRIAGE_ACTOR``),
    a session parking its own work, a person. Goes through ``set_hold``, so the
    permission gate and the fingerprint stay identical for all of them."""
    set_hold(wi, kind="evidence", reason=reason, actor=actor, now=now)


def held_refs(db: "Session") -> frozenset[str]:
    """``owner/repo#N`` of every open issue parked by an owner_action or decision
    hold — what the runner's sweep reads to drop ``gave_up`` rows that are no longer
    backed by a hold."""
    from sqlalchemy import select

    from server.pm.desired_state import HELD_KINDS
    rows = db.execute(select(WorkItem.repo, WorkItem.number).where(
        WorkItem.kind == "issue", WorkItem.state == "open",
        WorkItem.hold_kind.in_(sorted(HELD_KINDS)))).all()
    return frozenset(f"{repo}#{number}" for repo, number in rows)


def _audit_release(db: "Session", wi: WorkItem, *, kind: str, signal: str, actor: str,
                   via: str = "reconciler") -> None:
    """ONE ``pm.hold.release`` row shape for every sync release path. *via* is the
    surface that released it — the reconcile pass by default, ``"runner"`` for the
    spawn's ``dispatch`` consumption."""
    from server.audit import redact_detail
    from server.db.models import AuditEvent
    db.add(AuditEvent(type="pm.hold.release", identity=actor, via=via,
                      detail=redact_detail({"workItemId": wi.id, "kind": kind,
                                            "actorClass": actor_class(actor), "signal": signal})))


def release_by_signals(db: "Session", *, human_edits: "Sequence[HumanEdit]",
                       now: datetime) -> list[Released]:
    """THE release rule for owner_action holds (spec §7), run once per reconcile
    pass: a human board edit to Ready/Backlog, or an issue body that no longer
    matches the fingerprint taken when the hold was set. The Retry endpoint is the
    third signal and calls ``clear_hold`` directly with the person's actor.
    Idempotent: a released item has no hold to release again.

    Deviation from spec §7: the board edit is not compared against ``hold_set_at`` —
    ``HumanEdit`` has no timestamp and the mirror fetch time is not threaded here. A
    hold written during the fetch that follows a human's move to Ready can be
    released by that pre-hold edit; recorded as a follow-up in spec §14."""
    from sqlalchemy import select
    released: list[Released] = []
    board_refs = {e.ref for e in human_edits
                  if e.field == "Status" and str(e.new) in RELEASING_BOARD_STATUSES}
    held = db.scalars(select(WorkItem).where(
        WorkItem.kind == "issue", WorkItem.hold_kind == "owner_action")).all()
    for wi in held:
        ref = f"{wi.repo}#{wi.number}"
        signal: str | None = None
        if ref in board_refs:
            signal = "board_edit"
        elif wi.hold_fingerprint and wi.hold_fingerprint != body_fingerprint(wi.title, wi.body):
            signal = "body_edit"
        if signal is None:
            continue
        clear_hold(wi, actor=RECONCILER_ACTOR)
        _audit_release(db, wi, kind="owner_action", signal=signal, actor=RECONCILER_ACTOR)
        released.append((wi, signal))
    db.flush()
    return released


def release_on_dispatch(db: "Session", refs: "Sequence[str]", *, now: datetime) -> list[str]:
    """A dispatch CONSUMES the ``stronger_model`` hold it was climbing for (spec
    §4): clear it on every issue of the batch that carries one, audited as
    ``dispatch``, and answer the refs that were released.

    Only ``stronger_model``: a person's ``owner_action`` and an agent's
    ``evidence`` hold are other parks, and a spawn is not their release signal
    (the batch can carry an issue held for a reason of its own). Idempotent — a
    re-run finds no hold to release — and blind to a malformed ref, like every
    other reader of ``split_ref``. Sync, like ``release_by_signals``: the
    orchestrator calls it under ``db.run_sync``.

    *now* is accepted for symmetry with ``release_by_signals`` (a release writes
    no timestamp — it BLANKS ``hold_set_at``), so both release rules keep one
    call shape.
    """
    from sqlalchemy import select, tuple_

    from server.pm.refs import split_ref
    parts = [p for p in (split_ref(r) for r in refs) if p is not None]
    if not parts:
        return []
    released: list[str] = []
    for wi in db.scalars(select(WorkItem).where(
            WorkItem.kind == "issue", WorkItem.hold_kind == "stronger_model",
            tuple_(WorkItem.repo, WorkItem.number).in_(parts))).all():
        clear_hold(wi, actor=RUNNER_ACTOR)
        _audit_release(db, wi, kind="stronger_model", signal="dispatch",
                       actor=RUNNER_ACTOR, via="runner")
        released.append(f"{wi.repo}#{wi.number}")
    db.flush()
    return released


def release_on_investigation(db: "Session", ref: str) -> bool:
    """An ended investigation took the CONVERT-TO-A-FIX exit (spec 2026-09-18 §5):
    clear the ``evidence`` hold on *ref* so the next refill re-claims the report as
    an ordinary implement candidate, audited as ``investigation``.

    The runner's third hold-release rule, deliberately alongside the other two
    rather than hand-rolled at the call site: same ``clear_hold`` +
    ``_audit_release(via="runner")`` shape, so "how can a hold end?" still has one
    answer per signal in one file.

    Only ``evidence``, and only the investigated issue itself — a batch sibling is
    closed as a duplicate by the exit that touched it, and a person's
    ``owner_action`` park is not this signal's to end. Returns whether a hold was
    actually released: idempotent (a re-run finds nothing) and blind to a malformed
    ref, exactly like :func:`release_on_dispatch`. Sync — the orchestrator calls it
    under ``db.run_sync``.
    """
    from sqlalchemy import select

    from server.pm.refs import split_ref
    parts = split_ref(ref)
    if parts is None:
        return False
    repo, number = parts
    wi = db.scalars(select(WorkItem).where(
        WorkItem.kind == "issue", WorkItem.repo == repo, WorkItem.number == number,
        WorkItem.hold_kind == "evidence")).first()
    if wi is None:
        return False
    clear_hold(wi, actor=RUNNER_ACTOR)
    _audit_release(db, wi, kind="evidence", signal="investigation",
                   actor=RUNNER_ACTOR, via="runner")
    db.flush()
    return True


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


async def release_via_api(db, wi: WorkItem, *, actor: str, identity: str, signal: str,
                          only_kind: str | None = None) -> str | None:
    """THE release-and-audit sequence behind every API tap that ends a hold (spec §7):
    the Retry endpoint (``signal="retry"``) and Implement now (``signal="implement"``).

    Refuses a ``decision`` hold with ``DecisionHoldError`` and lets ``HoldPermissionError``
    out of ``clear_hold``; both routes map them to 422 ``bad_hold`` / ``hold_not_permitted``.
    ``only_kind`` narrows the tap to one kind: dispatch passes ``"owner_action"`` so an
    ``evidence`` / ``stronger_model`` hold — which has no release path there yet and exits
    on its own — is left untouched and returns ``None`` instead of failing the request.

    Returns the kind that WAS released, or ``None`` when nothing was (unheld item, or a
    kind ``only_kind`` skipped). Audits ``pm.hold.release`` with ``via="api"`` only when a
    hold actually ended."""
    from server.audit import record_audit
    refuse_decision_release(wi)
    if only_kind is not None and wi.hold_kind is not None and wi.hold_kind != only_kind:
        return None
    released = wi.hold_kind
    clear_hold(wi, actor=actor)
    if released is None:
        return None
    await db.flush()
    await record_audit(db=db, type="pm.hold.release", identity=identity,
                       detail={"workItemId": wi.id, "kind": released,
                               "actorClass": actor_class(actor), "signal": signal},
                       via="api")
    return released


def actor_for_request(request: object) -> str:
    """The actor string for an HTTP caller (spec §8): the drawer's bound token is
    ``assistant:<identity>``; otherwise the verified identity the middleware attached,
    which is ``lan@local`` for an unattributed loopback caller and a Tailscale login
    for a person. Takes any object with ``.state.identity`` / ``.state.via`` so the
    pure test needs no FastAPI Request."""
    state = getattr(request, "state", None)
    identity = str(getattr(state, "identity", "") or OPEN_LAN_ACTOR)
    if getattr(state, "via", None) == "assistant":
        return f"{ASSISTANT_PREFIX}{identity}"
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
