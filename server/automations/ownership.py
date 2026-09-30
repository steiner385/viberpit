"""Canonical durable exclusion for conflicting automation mutations.

Only trusted internal callers use this store. Resolve GitHub node IDs *before*
acquisition; repository names, display refs, sources and hosts never define locks.

``async with acquire(...) as tx`` atomically acquires every target or raises.
``async with owned(token) as tx`` rejects old/released/transferred tokens before
exposing ``tx.db``. Write associated AutomationTargetState rows through that same
session, never a second context. Both contexts commit on successful exit and roll
back on failure. A conflict is distinct from DB failure; neither permits effects.

Keep these transactions short: no network/process awaits, and never commit/rollback
``tx.db`` yourself. Effects start only AFTER successful context exit. The real
create_session chokepoint must call reserve_session exactly once before any clone
or spawn. A retry finding launching/uncertain must inspect the reserved Session
primary key or report uncertainty; it must NEVER run create_session again. Binding
may accompany insertion of that Session through tx.db in the same transaction.

ReleaseProof is a server-internal attestation after lifecycle reconciliation, not
an HTTP input, elapsed timer, or merely a 'complete' session status. The store can
prove NOT_STARTED only before its start transition; after that the caller must
prove native/external work has stopped. Unknown outcomes retain ownership forever.
"""
from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import Enum

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.db import db_session, dialect_insert
from server.db.models import AutomationOperation, AutomationOwnedTarget, Session as DbSession


@dataclass(frozen=True)
class CanonicalTarget:
    """Resolved github.com item identity; display spelling is not identity.

    Node IDs are opaque: validate their transport alphabet, never decode them or
    infer a node from a number. The resolver must verify item type/authority.
    """

    node_id: str
    display_ref: str

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_+=/-]+", self.node_id) or "#" in self.node_id:
            raise ValueError("a resolved GitHub item node ID is required")
        if not self.display_ref.strip():
            raise ValueError("a display reference is required")

    @property
    def key(self) -> str:
        return f"github:{self.node_id}"


class OwnershipConflict(RuntimeError):
    """At least one requested target is already owned; none were acquired."""


class StaleOwnership(RuntimeError):
    """The token no longer owns all of its targets; no writes are permitted."""


class ReleaseProof(Enum):
    NOT_STARTED = "not_started"
    WORK_STOPPED = "work_stopped"


async def _lock_targets(db: AsyncSession, keys: Sequence[str]) -> list[AutomationOwnedTarget]:
    # Insert first, even when rows exist: SQLite gets a write transaction BEFORE
    # reading; Postgres resolves insert conflicts then locks each row. Uniform
    # sorted ordering prevents cycles for overlapping multi-target acquisitions.
    rows = []
    for key in sorted(keys):
        await db.execute(dialect_insert(AutomationOwnedTarget).values(key=key)
                         .on_conflict_do_nothing(index_elements=["key"]))
        rows.append((await db.scalars(select(AutomationOwnedTarget).where(
            AutomationOwnedTarget.key == key).with_for_update())).one())
    return rows


def _pending_execution(*, stopped: bool = False) -> dict:
    """Canonical durable native-not-entered state for roots and children."""
    return {"current": "pending", "executions": {
        "pending": {"generation": 0, "stopped": 0 if stopped else None}}}


class OwnershipTransaction:
    """Valid only within its acquire/owned context. db is the retry-state seam."""

    def __init__(self, db: AsyncSession, operation: AutomationOperation,
                 targets: list[AutomationOwnedTarget]) -> None:
        self.db = db
        self.operation = operation
        self._targets = targets
        self._open = True

    @property
    def token(self) -> str:
        return self.operation.token

    def _check(self) -> None:
        if not self._open or any(t.operation_token != self.token for t in self._targets):
            raise StaleOwnership("ownership transaction is no longer current")

    def reserve_session(self) -> str:
        """One-shot launch CAS; commit before clone/spawn, never call on RPC retry."""
        self._check()
        if self.operation.phase != "claimed":
            raise ValueError("launch already started; reconcile the reserved session")
        session_id = str(uuid.uuid4())
        self.operation.reserved_session_id = session_id
        self.operation.execution_generation = 0
        self.operation.execution_sessions = {session_id: _pending_execution()}
        self.operation.phase = "launching"
        return session_id

    def start_action(self, *, session_id: str | None = None) -> None:
        """Commit before mechanical mutation or injection into an existing session."""
        self._check()
        if self.operation.phase != "claimed":
            raise ValueError("action already started; reconcile existing work")
        self.operation.session_id = session_id
        self.operation.phase = "active"

    def bind_session(self, session_id: str) -> None:
        self._check()
        if (self.operation.phase == "active"
                and self.operation.session_id == session_id
                and self.operation.reserved_session_id == session_id):
            return  # the recovery observer acknowledged this exact reservation first
        if (self.operation.phase not in ("launching", "uncertain")
                or session_id != self.operation.reserved_session_id):
            raise ValueError("only the reserved launch session may be bound")
        self.operation.session_id = session_id
        self.operation.phase = "active"

    def complete_action(self) -> None:
        """Record an observed completed non-session effect while retaining targets."""
        self._check()
        if (self.operation.phase not in ("active", "uncertain")
                or self.operation.session_id is not None
                or self.operation.reserved_session_id is not None):
            raise ValueError("only a non-session action can finish this way")
        self.operation.phase = "completed"

    def mark_uncertain(self, reason: str) -> None:
        self._check()
        if self.operation.phase not in ("launching", "active", "uncertain"):
            raise ValueError("no external action has started")
        if not reason.strip():
            raise ValueError("uncertainty needs a reason")
        self.operation.phase = "uncertain"
        self.operation.reason = reason

    def _validate_release(self, proof: ReleaseProof, reason: str) -> None:
        self._check()
        if not reason.strip():
            raise ValueError("release/handoff needs an observed reason")
        if proof is ReleaseProof.NOT_STARTED and self.operation.phase == "claimed":
            return
        if proof is ReleaseProof.WORK_STOPPED:
            if self.operation.session_id or self.operation.reserved_session_id:
                if (self.operation.execution_generation is None
                        or self.operation.stopped_generation != self.operation.execution_generation):
                    raise ValueError("native session execution has no current stopped-work attestation")
            return
        raise ValueError("possible external effects require proven stopped work")

    def release(self, proof: ReleaseProof, reason: str) -> None:
        self._validate_release(proof, reason)
        self.operation.phase = "released"
        self.operation.reason = reason
        for target in self._targets:
            target.operation_token = None

    async def transfer(self, *, owner: str, source: str, proof: ReleaseProof,
                       reason: str, provenance: dict | None = None,
                       successor_targets: Sequence[CanonicalTarget] | None = None,
                       action_started: bool = False) -> str:
        """Atomic handoff to a new fenced attempt, only after prior work is safe."""
        self._validate_release(proof, reason)
        if not owner.strip() or not source.strip():
            raise ValueError("owner and source are required")
        refs = dict(self.operation.display_refs)
        if successor_targets is not None:
            refs.update({target.key: target.display_ref for target in successor_targets})
        if set(refs) != {target.key for target in self._targets}:
            raise ValueError("successor targets must already be locked as one sorted union")
        successor = AutomationOperation(
            token=str(uuid.uuid4()), owner=owner, source=source,
            targets=sorted(refs), display_refs=refs,
            provenance=dict(provenance or {}),
            phase="active" if action_started else "claimed", reason=reason,
            predecessor_token=self.token,
        )
        self.db.add(successor)
        await self.db.flush()
        self.operation.phase = "transferred"
        self.operation.reason = reason
        for target in self._targets:
            target.operation_token = successor.token
        return successor.token


@asynccontextmanager
async def acquire(targets: Sequence[CanonicalTarget], *, owner: str, source: str,
                  reason: str, provenance: dict | None = None) -> AsyncIterator[OwnershipTransaction]:
    if not targets or not owner.strip() or not source.strip() or not reason.strip():
        raise ValueError("targets, owner, source and reason are required")
    if any(not isinstance(target, CanonicalTarget) for target in targets):
        raise ValueError("targets must be resolved canonical identities")
    refs = {target.key: target.display_ref for target in targets}
    async with db_session() as db:
        rows = await _lock_targets(db, sorted(refs))
        if any(row.operation_token is not None for row in rows):
            raise OwnershipConflict("target already has an operation")
        operation = AutomationOperation(
            token=str(uuid.uuid4()), owner=owner, source=source, targets=sorted(refs),
            display_refs=refs, provenance=dict(provenance or {}), phase="claimed", reason=reason)
        db.add(operation)
        await db.flush()
        for row in rows:
            row.operation_token = operation.token
        tx = OwnershipTransaction(db, operation, rows)
        try:
            yield tx
        finally:
            tx._open = False


@asynccontextmanager
async def owned(token: str) -> AsyncIterator[OwnershipTransaction]:
    async with db_session() as db:
        operation = await db.get(AutomationOperation, token)
        if operation is None or not operation.targets:
            raise StaleOwnership("unknown ownership token")
        rows = await _lock_targets(db, operation.targets)
        if any(row.operation_token != token for row in rows):
            raise StaleOwnership("operation no longer owns its targets")
        # A concurrent holder can have changed phase while this context waited
        # for the target locks. Do not use the earlier identity-map snapshot.
        await db.refresh(operation)
        tx = OwnershipTransaction(db, operation, rows)
        try:
            yield tx
        finally:
            tx._open = False


async def holders_for_refs(refs: Sequence[str]) -> list[AutomationOperation]:
    """Read-only: every operation currently holding a target whose display ref
    is one of *refs* (case-insensitive), newest first.

    Diagnostic only — it never acquires, transfers or releases, and it matches
    on the display spelling a refusal was raised for rather than re-resolving
    node IDs, so a refused claim can name its holder without a second GitHub
    round trip. Identity for exclusion stays the canonical key (``acquire``).
    """
    wanted = {ref.lower() for ref in refs}
    if not wanted:
        return []
    async with db_session() as db:
        operations = (await db.scalars(
            select(AutomationOperation)
            .join(AutomationOwnedTarget,
                  AutomationOwnedTarget.operation_token == AutomationOperation.token)
            .order_by(AutomationOperation.created_at.desc())
            .distinct())).all()
    return [op for op in operations
            if wanted & {ref.lower() for ref in (op.display_refs or {}).values()}]


@dataclass(frozen=True)
class ExecutionSnapshot:
    token: str
    session_id: str
    generation: int | None
    tree_generation: int | None
    stopped_generation: int | None
    tree_stopped_generation: int | None
    execution_id: str | None


async def execution_snapshot(session_id: str, *, parent_session_id: str | None = None,
                             execution_id: str | None = None) -> ExecutionSnapshot | None:
    """Find the one operation for this session or its trusted ancestor lineage.

    The parent hint comes only from the canonical pre-launch SessionRecord, while
    creation has not yet inserted its DB row. Public input cannot supply it.
    """
    async with db_session() as db:
        current, seen = session_id, set()
        while current:
            if current in seen:
                raise StaleOwnership("cyclic session ownership ancestry")
            seen.add(current)
            operations = (await db.scalars(select(AutomationOperation).where(or_(
                AutomationOperation.session_id == current,
                AutomationOperation.reserved_session_id == current,
                AutomationOperation.execution_sessions[current]["current"].as_string().is_not(None),
            )).order_by(AutomationOperation.created_at.desc()))).all()
            if operations:
                active = [op for op in operations if op.phase not in ("released", "transferred")]
                if len(active) > 1:
                    raise StaleOwnership("session belongs to multiple active operations; explicit handoff required")
                operation = active[0] if active else operations[0]
                session = (operation.execution_sessions or {}).get(session_id, {})
                selected = execution_id or session.get("current")
                state = session.get("executions", {}).get(selected, {})
                return ExecutionSnapshot(operation.token, session_id, state.get("generation"),
                                         operation.execution_generation, state.get("stopped"),
                                         operation.stopped_generation, selected)
            if current == session_id and parent_session_id:
                current = parent_session_id
            else:
                current = await db.scalar(select(DbSession.parent_session_id).where(
                    DbSession.id == current))
        return None


async def session_execution(session_id: str) -> tuple[str, int | None, int | None] | None:
    snapshot = await execution_snapshot(session_id)
    if snapshot is None:
        return None
    return snapshot.token, snapshot.tree_generation, snapshot.tree_stopped_generation


async def register_child_execution(session_id: str, parent_session_id: str) -> None:
    """Durably distinguish a new pending child from unknown legacy child history."""
    snapshot = await execution_snapshot(parent_session_id)
    if snapshot is None:
        return
    async with owned(snapshot.token) as tx:
        states = dict(tx.operation.execution_sessions or {})
        if session_id in states:
            raise StaleOwnership("child execution was already registered")
        states[session_id] = _pending_execution()
        tx.operation.execution_sessions = states
        tx.operation.execution_generation = (tx.operation.execution_generation or 0) + 1
        tx.operation.stopped_generation = None


async def begin_session_execution(session_id: str, *, parent_session_id: str | None = None,
                                  execution_id: str | None = None) -> None:
    """Invalidate the session AND whole-tree proof before managed native effects."""
    snapshot = await execution_snapshot(session_id, parent_session_id=parent_session_id)
    if snapshot is None:
        return
    async with owned(snapshot.token) as tx:
        generation = (tx.operation.execution_generation or 0) + 1
        tx.operation.execution_generation = generation
        tx.operation.stopped_generation = None
        states = dict(tx.operation.execution_sessions or {})
        session = dict(states.get(session_id, {}))
        selected = execution_id or session.get("current") or session_id
        if selected == "pending":
            selected = session_id
        executions = dict(session.get("executions", {}))
        executions.pop("pending", None)  # the one-shot native-entry boundary
        executions[selected] = {"generation": generation, "stopped": None}
        states[session_id] = {"current": selected, "executions": executions}
        tx.operation.execution_sessions = states


async def attest_session_stopped(token: str, generation: int, *, session_id: str | None = None,
                                 execution_id: str | None = None, allow_tree_stop: bool = True) -> bool:
    """CAS one native execution's proof; aggregate only when the whole tree stopped.

    A different child may advance while this close awaits. Its state remains
    active; it does not invalidate evidence about this frozen driver's lifetime.
    A restart of THIS session invalidates its own generation and rejects the CAS.
    """
    async with owned(token) as tx:
        root = tx.operation.session_id or tx.operation.reserved_session_id
        session_id = session_id or root
        states = dict(tx.operation.execution_sessions or {})
        session = dict(states.get(session_id, {}))
        selected = execution_id or session.get("current")
        executions = dict(session.get("executions", {}))
        state = executions.get(selected, {})
        if session_id is None or state.get("generation") != generation:
            return False
        if selected == "pending":
            row = await tx.db.get(DbSession, session_id)
            if generation != 0 or row is None or row.status not in ("ended", "archived"):
                return False  # unknown/missing/in-flight launch is not cancelled
        executions[selected] = {"generation": generation, "stopped": generation}
        states[session_id] = {**session, "executions": executions}
        tx.operation.execution_sessions = states
        if not allow_tree_stop:
            return True
        await _aggregate_tree_stopped(tx, root)
        return True


async def _aggregate_tree_stopped(tx: "OwnershipTransaction", root: str | None) -> None:
    """Recompute the whole-tree stop aggregate from *tx.operation*'s currently
    retained executions, setting ``stopped_generation`` when every one of them
    proves stopped. Shared by ``attest_session_stopped`` (as a side effect of a
    fresh per-execution CAS) and ``reconcile_tree_stopped`` (standalone, when no
    fresh CAS is coming)."""
    states = dict(tx.operation.execution_sessions or {})
    pending, seen = [root] if root else [], set()
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        current_state = states.get(current, {})
        pending_execution = current_state.get("executions", {}).get("pending", {})
        if pending_execution.get("generation") == 0:
            row = await tx.db.get(DbSession, current)
            if row is not None and row.status in ("ended", "archived"):
                # This child was registered before launch, and the required
                # native-entry generation write never happened. Terminal row
                # PLUS that durable not-started record proves no native effect.
                states[current] = _pending_execution(stopped=True)
        pending.extend((await tx.db.scalars(select(DbSession.id).where(
            DbSession.parent_session_id == current))).all())
    tx.operation.execution_sessions = dict(states)
    # Unknown legacy children and queued children with no execution evidence
    # retain ownership. Completed children retain their OWN proof when their
    # parent subsequently runs another turn, rather than becoming unknown.
    if (root and seen.issubset(states) and states
            and all(session.get("executions") for session in states.values())
            and all(value.get("generation") is not None
                    and value.get("generation") == value.get("stopped")
                    for session in states.values() for value in session["executions"].values())):
        tx.operation.stopped_generation = tx.operation.execution_generation


async def reconcile_absent_session_stopped(token: str, session_id: str) -> bool:
    """Force the stop proof for every execution retained under *session_id*
    whose DB row the CALLER has already independently confirmed no longer
    exists at all (never called on a merely terminal/archived row — a
    ``done`` row can still be resumed, so terminality alone proves nothing).

    A resumed session superseding its own predecessor execution is NOT proof
    that predecessor stopped — the predecessor may genuinely still be running,
    unaccounted for (see ``test_new_driver_cannot_erase_unproven_predecessor_lifetime``),
    so ``begin_session_execution`` deliberately never assumes it. A
    PERMANENTLY DELETED row is a categorically stronger signal than
    supersession: nothing can still be interacting with this target through
    *session_id*, because the very row any process would reference to persist
    anything for it no longer exists anywhere — the same proof
    ``_release_orphaned_claim`` already trusted for its own release call, just
    never previously reflected onto the tree's per-execution history, so a
    stale predecessor generation (killed by an earlier restart, never
    individually attested) permanently blocked the aggregate this operation's
    ``ReleaseProof.WORK_STOPPED`` depends on.

    Scoped to exactly the session the caller has proven absent — every OTHER
    session recorded in the tree keeps needing its own real proof. Returns
    whether the whole tree now proves stopped.
    """
    async with owned(token) as tx:
        states = dict(tx.operation.execution_sessions or {})
        session = dict(states.get(session_id, {}))
        executions = dict(session.get("executions", {}))
        touched = False
        for exec_id, state in executions.items():
            if exec_id == "pending":
                continue
            if state.get("stopped") is None and state.get("generation") is not None:
                executions[exec_id] = {**state, "stopped": state["generation"]}
                touched = True
        if touched:
            states[session_id] = {**session, "executions": executions}
            tx.operation.execution_sessions = states
        root = tx.operation.session_id or tx.operation.reserved_session_id
        await _aggregate_tree_stopped(tx, root)
        return tx.operation.stopped_generation == tx.operation.execution_generation


async def attest_terminal_session_stopped(session_id: str) -> bool:
    """Attest EVERY unstopped native execution retained for *session_id*, then let
    the tree aggregate (#2009).

    For a session whose driver the owning orchestrator has already torn down — its
    row is ``archived``/``ended`` — each retained generation names a process that no
    longer exists, whatever its close-time probe could observe: a turn still live
    when ``mark_complete`` released the driver, or a resumed driver whose
    predecessor died under a drain cap and so can never prove itself. One CAS per
    execution, through the same path a live close uses, so a concurrent restart of
    the session still rejects it. Returns True iff the whole managed tree is
    stopped afterwards.
    """
    snapshot = await execution_snapshot(session_id)
    if snapshot is None:
        return False
    async with db_session() as db:
        operation = await db.get(AutomationOperation, snapshot.token)
        states = (operation.execution_sessions or {}) if operation is not None else {}
    executions = dict(states.get(session_id, {}).get("executions", {}))
    for execution_id, state in executions.items():
        generation = state.get("generation")
        if generation is None or state.get("stopped") is not None:
            continue
        await attest_session_stopped(snapshot.token, generation, session_id=session_id,
                                     execution_id=execution_id)
    association = await session_execution(session_id)
    return bool(association and association[1] is not None and association[1] == association[2])
