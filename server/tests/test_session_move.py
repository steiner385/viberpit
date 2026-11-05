"""Move to host (D-17): the movable predicate, the edge saga against two fake fleet
hosts, the crash reconcile, the daemon halves (``transfer_*``) against real git with a
local bare origin, the split-brain gaps, the edge guard and the route.

Plan: docs/superpowers/plans/2026-11-05-desktop-parity-d17-move-session.md (Task 1).
Facts: docs/superpowers/specs/2026-11-05-d17-move-session-verification.md.
Never touches GitHub: visibility is stubbed and every origin is a local bare repo."""
from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from viberpit import gitrepos, worktree_transfer as wt, worktrees
from server.codex.capabilities import CODEX_CAPABILITIES
from server.db import db_session
from server.db.clock import naive_utc_now
from server.db.models import AuditEvent, Host, Session as DbSession
from server.sessions import session_lease, session_move as sm
from server.sessions.claude_driver import ClaudeAgentDriver
from server.sessions.engine import AgentEngine, capabilities_to_wire
from server.sessions.fleet import HostCandidate
from server.sessions.fleet_orchestrator import FleetOrchestrator
from server.sessions.fleet_router import FleetRouter
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.types import SessionRecord

SID = "5e55a0aa-1111-2222-3333-444455556666"
BOTH = frozenset({"dobby", "rocky"})


# ═════════════════════════════════════════════════════════════════════════════════════
# The movable predicate (pure)
# ═════════════════════════════════════════════════════════════════════════════════════

def _subject(**kw) -> sm.MoveSubject:
    base = dict(session_id=SID, engine=AgentEngine.CLAUDE_CODE, kind=None,
                display_mode="virtual", status="done", source="dobby")
    base.update(kw)
    return sm.MoveSubject(**base)


def _cand(name="rocky", **kw) -> HostCandidate:
    base = dict(name=name, enabled=True, healthy=True, weight=100, active_sessions=1,
                max_sessions=6, load_avg=0.5, mem_available_mb=8000)
    base.update(kw)
    return HostCandidate(**base)


def _verdict(subject=None, target="rocky", cand="default", **kw) -> sm.MoveVerdict:
    caps = kw.pop("caps", ClaudeAgentDriver.capabilities)
    return sm.movable(subject or _subject(), target, _cand() if cand == "default" else cand,
                      caps=caps, enabled=kw.pop("enabled", True), **kw)


def test_an_idle_claude_session_is_movable():
    assert _verdict().ok


@pytest.mark.parametrize("subject_kw,code", [
    ({"kind": "assistant"}, sm.MOVE_ASSISTANT),
    ({"display_mode": "native"}, sm.MOVE_NATIVE_DISPLAY),
    ({"status": "in_progress"}, sm.MOVE_MID_TURN),
    ({"status": "condensing"}, sm.MOVE_MID_TURN),
    ({"status": "retrying"}, sm.MOVE_MID_TURN),
    ({"status": "queued"}, sm.MOVE_NOT_STARTED),
    ({"status": "pending_approval"}, sm.MOVE_NOT_STARTED),
    ({"status": "archived"}, sm.MOVE_INACTIVE),
    ({"status": "ended"}, sm.MOVE_INACTIVE),
    ({"live_children": 2}, sm.MOVE_HAS_CHILDREN),
    ({"live_parent": "parent-1"}, sm.MOVE_IS_CHILD),
    ({"managed": False}, sm.MOVE_NOT_MANAGED),
    ({"moving_to": "rocky"}, sm.MOVE_IN_FLIGHT),
    ({"source": "rocky"}, sm.MOVE_SAME_HOST),
])
def test_session_level_refusals(subject_kw, code):
    v = _verdict(_subject(**subject_kw))
    assert not v.ok and v.code == code and v.message


def test_a_codex_session_is_refused_with_the_history_explanation():
    v = _verdict(_subject(engine=AgentEngine.CODEX), caps=CODEX_CAPABILITIES)
    assert v.code == sm.MOVE_ENGINE_UNSUPPORTED
    assert "Codex history stays on its host" in v.message


def test_the_kill_switch_refuses_everything():
    assert _verdict(enabled=False).code == sm.MOVE_DISABLED


@pytest.mark.parametrize("cand,code", [
    (None, sm.MOVE_UNKNOWN_HOST),
    (_cand(enabled=False), sm.MOVE_TARGET_DISABLED),
    (_cand(healthy=None), sm.MOVE_TARGET_UNHEALTHY),
    (_cand(healthy=False), sm.MOVE_TARGET_UNHEALTHY),
    (_cand(active_sessions=6, max_sessions=6), sm.MOVE_TARGET_FULL),
])
def test_target_refusals(cand, code):
    assert _verdict(cand=cand).code == code


def test_a_platform_session_needs_a_host_advertising_it():
    v = _verdict(_subject(platform="windows"))
    assert v.code == sm.MOVE_TARGET_PLATFORM


def test_claude_declares_cross_host_transfer_and_codex_does_not(monkeypatch):
    assert ClaudeAgentDriver.capabilities.cross_host_transfer is True
    assert CODEX_CAPABILITIES.cross_host_transfer is False
    assert capabilities_to_wire(ClaudeAgentDriver.capabilities)["crossHostTransfer"] is True
    assert capabilities_to_wire(CODEX_CAPABILITIES)["crossHostTransfer"] is False
    monkeypatch.setenv("VIBERPIT_SESSION_MOVE_ENABLED", "false")
    assert capabilities_to_wire(ClaudeAgentDriver.capabilities)["crossHostTransfer"] is False


# Revised D1 (owner, 2026-11-05): a dirty tree on a public repo never travels.
@pytest.mark.parametrize("private,carry,code", [
    (False, True, sm.MOVE_DIRTY_PUBLIC_REPO),
    (None, True, sm.MOVE_DIRTY_PUBLIC_REPO),       # unknown visibility = public
    (True, False, sm.MOVE_DIRTY_NEEDS_CONSENT),
    (True, True, None),
])
def test_dirty_tree_rules(private, carry, code):
    facts = sm.WorktreeFacts(dirty_count=3, dirty_paths=("a", "b", "c"), repo_private=private)
    v = _verdict(facts=facts, carry_dirty=carry)
    assert v.code == code
    if code is not None:
        assert v.extra["dirtyPaths"] == ["a", "b", "c"] and v.extra["dirtyCount"] == 3


# Final review ruling: committed-but-unpushed work on a public repo never travels either.
@pytest.mark.parametrize("private,code", [
    (False, sm.MOVE_UNPUSHED_PUBLIC_REPO),
    (None, sm.MOVE_UNPUSHED_PUBLIC_REPO),          # unknown visibility = public
    (True, None),
])
def test_unpushed_commit_rules(private, code):
    facts = sm.WorktreeFacts(unpushed_commits=2, repo_private=private)
    v = _verdict(facts=facts)
    assert v.code == code
    if code is not None:
        assert v.message == sm.UNPUSHED_PUBLIC_REPO_COPY
        assert v.extra["unpushedCommits"] == 2
    assert _verdict(facts=sm.WorktreeFacts(repo_private=private)).ok   # none: fine


def test_background_tasks_need_a_confirm_that_lists_them():
    tasks = ({"kind": "monitor", "name": "Monitor", "description": "watch CI",
              "replayable": True},)
    facts = sm.WorktreeFacts(background_tasks=tasks)
    v = _verdict(facts=facts)
    assert v.code == sm.MOVE_BACKGROUND_NEEDS_CONFIRM
    assert v.extra["backgroundTasks"] == list(tasks)
    assert _verdict(facts=facts, stop_background=True).ok


def test_only_the_current_worktree_base_counts_as_managed(tmp_path):
    root = str(tmp_path / "GitHub")
    base = worktrees.worktrees_base(root)
    assert sm.is_under_current_base(str(base / "viberpit" / "abcd1234"), root)
    from viberpit import paths
    for legacy in paths.repo_namespace_dirs(root):
        if legacy != base.parent:
            assert not sm.is_under_current_base(
                str(legacy / "worktrees" / "viberpit" / "abcd1234"), root)
    assert not sm.is_under_current_base(str(base.parent / "scratch" / "abcd1234"), root)
    assert not sm.is_under_current_base(str(base / "viberpit"), root)
    assert not sm.is_under_current_base(None, root)


# ═════════════════════════════════════════════════════════════════════════════════════
# Transcript marker: the source records it, the target verifies it (sshfs staleness)
# ═════════════════════════════════════════════════════════════════════════════════════

def _write_transcript(path: Path, uuids: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps({"type": "user", "uuid": u, "message": "x" * 20}) + "\n"
                            for u in uuids))


def test_transcript_marker_and_check(tmp_path):
    p = tmp_path / "t.jsonl"
    _write_transcript(p, ["u1", "u2", "u3"])
    marker = sm.transcript_marker(str(p))
    assert marker == {"size": p.stat().st_size, "lastUuid": "u3"}
    assert sm.transcript_satisfies(str(p), marker)
    stale = tmp_path / "stale.jsonl"
    _write_transcript(stale, ["u1", "u2"])            # the source's last turn is missing
    assert not sm.transcript_satisfies(str(stale), marker)
    assert not sm.transcript_satisfies(None, marker)
    assert sm.transcript_satisfies(None, None)         # nothing was ever written
    assert sm.transcript_marker(str(tmp_path / "missing.jsonl")) is None


# ═════════════════════════════════════════════════════════════════════════════════════
# The edge saga against two fake fleet hosts
# ═════════════════════════════════════════════════════════════════════════════════════

async def _lease_holder(sid: str = SID) -> str | None:
    state = await session_lease.current(sid)
    if state is None or state.age_s >= 120:
        return None
    return state.holder


async def _release(sid: str, holder: str) -> None:
    state = await session_lease.current(sid)
    if state is not None and state.holder == holder:
        await session_lease.release(sid, holder, state.fence)


class FakeHost:
    """One fleet daemon for the saga: in-memory records, worktrees and live drivers;
    the lease is the REAL session_lease row in the shared test DB; transfer refs live in
    a shared ``origin`` set. Mirrors the real daemon's ordering rules (import requires
    the row stamped here; finalize refuses a row stamped here)."""

    def __init__(self, name: str, *, role: str, origin: set[str]) -> None:
        self.name, self.role, self.origin = name, role, origin
        self.records: dict[str, SessionRecord] = {}
        self.trees: set[str] = set()
        self.drivers: set[str] = set()
        self.fail: dict[str, BaseException] = {}
        self.calls: list[str] = []
        self.gate: asyncio.Event | None = None
        self.preflight: dict = {}
        self.events: list[tuple[str, str]] = []

    def seed(self, sid: str = SID) -> None:
        self.records[sid] = SessionRecord(
            id=sid, name="s", account="acct", engine=AgentEngine.CLAUDE_CODE,
            engine_session_id="c-1", cols=80, rows=24, cwd="/wt/viberpit/5e55a0aa",
            detached=False, status="done", host=self.name)
        self.trees.add(sid)
        self.drivers.add(sid)

    async def get_session(self, sid):
        return self.records.get(sid)

    async def ensure_driver(self, sid, *a, **k):
        return object() if sid in self.records else None

    async def transfer_preflight(self, sid):
        self.calls.append("preflight")
        return {"present": sid in self.trees, "dirtyCount": 0, "dirtyPaths": [],
                "repoPrivate": None, "backgroundTasks": [], **self.preflight}

    async def transfer_export(self, sid, *, target, carry_dirty=False, stop_background=False):
        self.calls.append("export")
        if "export_refuse" in self.fail:
            raise self.fail["export_refuse"]
        self.drivers.discard(sid)
        await _release(sid, self.name)
        if "export" in self.fail:
            raise self.fail["export"]
        self.origin.add(sid)
        if self.role == "worker":
            self.records.pop(sid, None)
        else:
            self.records[sid].host = target
        return {"sessionId": sid, "source": self.name, "bundle": {"sessionId": sid},
                "transcript": {"size": 10, "lastUuid": "u"}, "preambles": []}

    async def transfer_import(self, sid, manifest=None, *, source=None, local=False):
        self.calls.append("restore" if local else "import")
        if self.gate is not None:
            await self.gate.wait()
        key = "restore" if local else "import"
        if key in self.fail:
            if local:                      # _abandon(local): close the driver here
                self.drivers.discard(sid)
                await _release(sid, self.name)
            raise self.fail[key]
        async with db_session() as db:
            row = await db.get(DbSession, sid)
        if row.host != self.name:
            raise sm.MoveError(sm.MOVE_NOT_STAMPED, f"stamped {row.host}")
        if local and sid not in self.trees:
            raise sm.MoveError(sm.MOVE_WORKTREE_MISSING)
        self.trees.add(sid)               # worktree first (import_ / reuse) …
        self.records[sid] = SessionRecord(  # … then the record, stamped here …
            id=sid, name="s", account="acct", engine=AgentEngine.CLAUDE_CODE,
            engine_session_id="c-1", cols=80, rows=24, cwd="/wt/viberpit/5e55a0aa",
            detached=False, status="done", host=self.name)
        if await session_lease.acquire(sid, self.name, ttl_s=120) is None:
            # … then the claim. A lost claim gives the record to the lease holder,
            # exactly as the daemon's _abandon does.
            holder = await _lease_holder(sid)
            if self.role == "worker":
                self.records.pop(sid, None)
            else:
                self.records[sid].host = holder
            if not local:
                self.trees.discard(sid)
            raise sm.MoveError(sm.MOVE_LEASE_HELD)
        self.drivers.add(sid)
        self.events.append((self.name, "restore" if local else "import"))
        # Like the daemon: the marker CAS only lands where this host is the claim's
        # target (a reconcile adopting a target that resumed outside the saga).
        await sm.mark_import_complete(sid, self.name)
        self.origin.discard(sid)          # delete_transfer_refs (best-effort, both paths)
        return {"sessionId": sid, "host": self.name, "driver": True}

    async def transfer_finalize(self, sid, manifest=None, *, owner):
        self.calls.append("finalize")
        self.events.append((self.name, "finalize"))
        if "finalize" in self.fail:
            raise self.fail["finalize"]
        async with db_session() as db:
            row = await db.get(DbSession, sid)
        if row.host == self.name:
            raise sm.MoveError(sm.MOVE_FINALIZE_REFUSED)
        self.drivers.discard(sid)
        await _release(sid, self.name)
        removed = sid in self.trees
        self.trees.discard(sid)
        if self.role == "worker":
            self.records.pop(sid, None)
        elif sid in self.records:
            self.records[sid].host = owner
        return {"sessionId": sid, "removed": removed,
                "recoveryRef": "refs/viberpit/recovery/5e55a0aa/x-forced" if removed else None}


@pytest.fixture
def fleet(isolated_db):
    """dobby (primary, the source: record, tree, live driver, lease) and rocky (worker),
    both enabled and healthy, behind a real FleetOrchestrator and the real DB."""
    origin: set[str] = set()
    dobby = FakeHost("dobby", role="primary", origin=origin)
    rocky = FakeHost("rocky", role="worker", origin=origin)
    dobby.seed()

    async def _seed():
        async with db_session() as db:
            db.add(DbSession(id=SID, name="s", account_name="acct", engine_session_id="c-1",
                             status="done", cwd="/wt/viberpit/5e55a0aa", host="dobby",
                             repo="acme/widget"))
            for name in ("dobby", "rocky"):
                db.add(Host(name=name, endpoint=f"https://{name}:9443", enabled=True,
                            healthy=True, weight=100, max_sessions=6, active_sessions=1))
            await db.commit()
        assert await session_lease.acquire(SID, "dobby", ttl_s=120) is not None
    asyncio.run(_seed())
    rocky.events = dobby.events                      # one ordered log for both hosts
    router = FleetRouter()
    router.add("dobby", dobby)
    router.add("rocky", rocky)
    f = FleetOrchestrator(router, primary="dobby")
    return SimpleFleet(f, dobby, rocky, origin)


class SimpleFleet:
    def __init__(self, f, dobby, rocky, origin):
        self.f, self.dobby, self.rocky, self.origin = f, dobby, rocky, origin


@pytest.fixture(autouse=True)
def _managed(monkeypatch):
    """The fake rows' cwd counts as a managed worktree under the current base."""
    real = sm.is_under_current_base
    monkeypatch.setattr(sm, "is_under_current_base",
                        lambda cwd, root: cwd == "/wt/viberpit/5e55a0aa" or real(cwd, root))


async def _row() -> DbSession:
    async with db_session() as db:
        row = await db.get(DbSession, SID)
    assert row is not None
    return row


async def _audits() -> list[dict]:
    async with db_session() as db:
        rows = (await db.execute(select(AuditEvent).where(AuditEvent.type == "session.move")
                                 .order_by(AuditEvent.at))).scalars().all()
    return [r.detail for r in rows]


async def _move(fx, **kw):
    return await sm.move_session(fx.f, SID, "rocky", identity="tony@example.com", **kw)


def _on_source_only(fx) -> None:
    """The session is usable on exactly one host: dobby (Review Focus 1)."""
    assert fx.dobby.records[SID].host == "dobby"
    assert SID not in fx.rocky.records
    assert SID in fx.dobby.trees and SID not in fx.rocky.trees
    assert fx.f._host_cache.get(SID) == "dobby"
    assert fx.f.moving_target(SID) is None


@pytest.mark.asyncio
async def test_happy_path_moves_and_finalizes_the_source(fleet):
    fx = fleet
    res = await _move(fx)
    assert res["outcome"] == sm.OUTCOME_MOVED and res["finalized"] is True
    assert res["recoveryRef"]
    row = await _row()
    assert (row.host, row.moved_from, row.moving_to) == ("rocky", "dobby", None)
    assert row.moved_at is not None and row.move_started_at is None
    assert await _lease_holder() == "rocky"
    assert fx.dobby.records[SID].host == "rocky"          # primary mirror, retargeted
    assert fx.rocky.records[SID].host == "rocky"
    assert SID in fx.rocky.trees and SID not in fx.dobby.trees
    assert fx.origin == set()                             # transfer refs deleted
    assert fx.f._host_cache[SID] == "rocky"
    assert fx.dobby.calls == ["preflight", "export", "finalize"]
    assert fx.rocky.calls == ["import"]
    [audit] = await _audits()
    assert audit["outcome"] == "moved" and audit["from"] == "dobby" and audit["to"] == "rocky"
    assert audit["finalized"] is True and audit["carryDirty"] is False


@pytest.mark.asyncio
async def test_predicate_refusal_touches_nothing_and_is_audited(fleet):
    fx = fleet
    fx.dobby.preflight = {"dirtyCount": 2, "dirtyPaths": ["a.py", "b.py"], "repoPrivate": True}
    with pytest.raises(sm.MoveError) as ei:
        await _move(fx)
    assert ei.value.code == sm.MOVE_DIRTY_NEEDS_CONSENT
    assert ei.value.extra["dirtyPaths"] == ["a.py", "b.py"]
    assert "export" not in fx.dobby.calls
    row = await _row()
    assert row.moving_to is None and row.host == "dobby"
    assert await _lease_holder() == "dobby"
    assert (await _audits())[-1]["outcome"] == "refused"


@pytest.mark.asyncio
async def test_public_repo_dirty_is_refused_even_with_consent(fleet):
    fx = fleet
    fx.dobby.preflight = {"dirtyCount": 1, "dirtyPaths": ["a.py"], "repoPrivate": False}
    with pytest.raises(sm.MoveError) as ei:
        await _move(fx, carry_dirty=True)
    assert ei.value.code == sm.MOVE_DIRTY_PUBLIC_REPO
    assert "export" not in fx.dobby.calls


@pytest.mark.asyncio
async def test_source_recheck_refusal_leaves_the_session_untouched(fleet):
    """A prompt landed between the predicate and the export: the source refuses inside
    its _swapping slot, before closing anything."""
    fx = fleet
    fx.dobby.fail["export_refuse"] = sm.MoveError(sm.MOVE_MID_TURN)
    with pytest.raises(sm.MoveError) as ei:
        await _move(fx)
    assert ei.value.code == sm.MOVE_MID_TURN and ei.value.outcome is None
    row = await _row()
    assert (row.host, row.moving_to) == ("dobby", None)
    assert await _lease_holder() == "dobby"
    assert (await _audits())[-1]["outcome"] == "refused"


@pytest.mark.asyncio
async def test_export_failure_restores_on_the_source(fleet):
    fx = fleet
    fx.dobby.fail["export"] = sm.MoveError(sm.MOVE_EXPORT_FAILED, "push_failed")
    with pytest.raises(sm.MoveError) as ei:
        await _move(fx)
    assert ei.value.code == sm.MOVE_EXPORT_FAILED and ei.value.outcome == "restored"
    row = await _row()
    assert (row.host, row.moving_to, row.moved_from) == ("dobby", None, None)
    assert await _lease_holder() == "dobby"
    _on_source_only(fx)
    assert fx.dobby.calls[-1] == "restore" and fx.rocky.calls == []
    assert fx.origin == set()
    assert (await _audits())[-1]["outcome"] == "restored"


@pytest.mark.asyncio
async def test_restamp_failure_restores_on_the_source(fleet, monkeypatch):
    fx = fleet
    real = sm._restamp

    async def flaky(sid, *, host, moved_from, moved_at):
        if host == "rocky":
            raise RuntimeError("db write failed")
        await real(sid, host=host, moved_from=moved_from, moved_at=moved_at)
    monkeypatch.setattr(sm, "_restamp", flaky)
    with pytest.raises(sm.MoveError) as ei:
        await _move(fx)
    assert ei.value.code == sm.MOVE_RESTAMP_FAILED and ei.value.outcome == "restored"
    row = await _row()
    assert (row.host, row.moving_to) == ("dobby", None)
    assert await _lease_holder() == "dobby"
    _on_source_only(fx)
    assert fx.origin == set()                 # the restore deleted the pushed refs
    assert fx.rocky.calls == []
    assert (await _audits())[-1]["code"] == sm.MOVE_RESTAMP_FAILED


@pytest.mark.parametrize("exc,code", [
    (RuntimeError("fetch failed"), sm.MOVE_IMPORT_FAILED),
    (sm.MoveError(sm.MOVE_TRANSCRIPT_STALE), sm.MOVE_TRANSCRIPT_STALE),
    (sm.MoveError(sm.MOVE_RESUME_FAILED), sm.MOVE_RESUME_FAILED),
])
@pytest.mark.asyncio
async def test_target_failure_restores_on_the_source(fleet, exc, code):
    """Import, transcript-stale and ensure_driver failures on the target all end with the
    session back on the source: stamp, lease, both in-memory records, both trees."""
    fx = fleet
    fx.rocky.fail["import"] = exc
    with pytest.raises(sm.MoveError) as ei:
        await _move(fx)
    assert ei.value.code == code and ei.value.outcome == "restored"
    row = await _row()
    assert (row.host, row.moving_to, row.moved_from) == ("dobby", None, None)
    assert await _lease_holder() == "dobby"
    _on_source_only(fx)
    assert fx.origin == set()
    assert "finalize" not in fx.dobby.calls          # D4: never before the target runs
    audit = (await _audits())[-1]
    assert audit["outcome"] == "restored" and audit["code"] == code


@pytest.mark.asyncio
async def test_finalize_failure_keeps_the_move_and_the_source_tree(fleet):
    fx = fleet
    fx.dobby.fail["finalize"] = RuntimeError("worktree remove failed")
    res = await _move(fx)
    assert res["outcome"] == "moved" and res["finalized"] is False
    row = await _row()
    assert (row.host, row.moving_to) == ("rocky", None)
    assert await _lease_holder() == "rocky"
    assert SID in fx.dobby.trees and SID in fx.rocky.trees     # a disk leak, not a twin
    assert fx.dobby.records[SID].host == "rocky"               # …and stamped elsewhere
    audit = (await _audits())[-1]
    assert audit["outcome"] == "moved" and audit["finalized"] is False


@pytest.mark.asyncio
async def test_restore_failure_is_failed_and_left_to_the_reconcile(fleet):
    """Fix round 1, item 3: a source unreachable during restore must not hand the
    session back half-restored. The claim stays (marked abandoned), driver-building
    calls keep answering SESSION_MOVING, and the reconcile finishes the restore: the
    WIP is unpacked, the refs deleted and the session runs on exactly one host."""
    fx = fleet
    fx.rocky.fail["import"] = RuntimeError("boom")
    fx.dobby.fail["restore"] = RuntimeError("source down too")
    with pytest.raises(sm.MoveError) as ei:
        await _move(fx)
    assert ei.value.outcome == "failed"
    row = await _row()
    assert (row.host, row.moving_to) == ("dobby", "rocky")     # left for the reconcile
    assert row.move_started_at is None                         # … marked abandoned
    with pytest.raises(sm.SessionMoving):
        await fx.f.ensure_driver(SID)
    assert (await _audits())[-1]["outcome"] == "failed"
    assert fx.origin == {SID}                                  # refs still published
    fx.dobby.fail.pop("restore")                               # the source is back
    assert await sm.reconcile_stale_moves(fx.f) == [SID]
    row = await _row()
    assert (row.host, row.moving_to) == ("dobby", None)
    assert fx.origin == set()
    _on_source_only(fx)
    assert await _lease_holder() == "dobby"
    assert await fx.f.ensure_driver(SID) is not None            # no longer refused


@pytest.mark.asyncio
async def test_restore_never_overrides_a_target_holding_the_lease(fleet):
    """The import answered too late for the edge, but the target IS driving the session:
    the lease is the fact, so the session stays there (no twin)."""
    fx = fleet
    real_import = fx.rocky.transfer_import

    async def late(sid, manifest=None, *, source=None, local=False):
        await real_import(sid, manifest, source=source, local=local)
        raise RuntimeError("edge gave up waiting")
    fx.rocky.transfer_import = late
    with pytest.raises(sm.MoveError) as ei:
        await _move(fx)
    assert ei.value.outcome == "moved"
    row = await _row()
    assert (row.host, row.moving_to) == ("rocky", None)
    assert await _lease_holder() == "rocky"
    # Fix round 1, item 2: the source's restore unpacked and installed a record before
    # its claim lost — no twin-capable copy may stay behind on it.
    assert "finalize" in fx.dobby.calls
    assert SID not in fx.dobby.trees and SID in fx.rocky.trees
    assert fx.dobby.records[SID].host == "rocky"
    assert SID in fx.rocky.drivers and SID not in fx.dobby.drivers


@pytest.mark.asyncio
async def test_a_second_move_is_refused_while_one_is_in_flight(fleet):
    fx = fleet
    fx.rocky.gate = asyncio.Event()
    first = asyncio.create_task(_move(fx))
    while "import" not in fx.rocky.calls:
        await asyncio.sleep(0.01)
    with pytest.raises(sm.MoveError) as ei:
        await _move(fx)
    assert ei.value.code == sm.MOVE_IN_FLIGHT
    fx.rocky.gate.set()
    assert (await first)["outcome"] == "moved"


@pytest.mark.asyncio
async def test_driver_building_calls_answer_session_moving_while_in_flight(fleet):
    fx = fleet
    fx.rocky.gate = asyncio.Event()
    task = asyncio.create_task(_move(fx))
    while "import" not in fx.rocky.calls:
        await asyncio.sleep(0.01)
    with pytest.raises(sm.SessionMoving) as ei:
        await fx.f.ensure_driver(SID)
    assert ei.value.target == "rocky" and ei.value.code == sm.SESSION_MOVING
    with pytest.raises(sm.SessionMoving):
        await fx.f.send_prompt(SID, "hi")
    rec = await fx.f.get_session(SID)                  # reads keep working …
    assert rec.moving_to == "rocky"                    # … and say where it is going
    assert (await _row()).moving_to == "rocky"
    fx.rocky.gate.set()
    await task
    assert (await fx.f.get_session(SID)).moving_to is None


@pytest.mark.asyncio
@pytest.mark.parametrize("name,args", [
    ("rename_session", ("new name",)), ("set_pr_autofix_paused", (True,)),
    ("set_auto_archive_opt_out", (True,)), ("dismiss_suspected_complete", ()),
    ("interrupt", ()), ("stop_subagent", ("tu-1",)),
])
async def test_live_record_writes_answer_session_moving_while_in_flight(fleet, name, args):
    """Final review: these write the source's live record (or its driver). Mid-move
    they must get the clean SESSION_MOVING 409, not land on a record about to go."""
    from server.sessions.fleet_orchestrator import _MOVE_BLOCKED, _PER_SESSION
    assert name in _MOVE_BLOCKED and name in _PER_SESSION
    fx = fleet
    fx.rocky.gate = asyncio.Event()
    task = asyncio.create_task(_move(fx))
    while "import" not in fx.rocky.calls:
        await asyncio.sleep(0.01)
    with pytest.raises(sm.SessionMoving) as ei:
        await getattr(fx.f, name)(SID, *args)
    assert ei.value.code == sm.SESSION_MOVING
    fx.rocky.gate.set()
    await task


@pytest.mark.asyncio
async def test_a_cancelled_request_does_not_leave_a_half_moved_session(fleet):
    fx = fleet
    fx.rocky.gate = asyncio.Event()
    caller = asyncio.create_task(_move(fx))
    while "import" not in fx.rocky.calls:
        await asyncio.sleep(0.01)
    caller.cancel()                                     # the HTTP request went away
    await asyncio.sleep(0.05)
    fx.rocky.gate.set()
    with pytest.raises(asyncio.CancelledError):
        await caller
    for _ in range(200):
        if fx.f.moving_target(SID) is None and "finalize" in fx.dobby.calls:
            break
        await asyncio.sleep(0.01)
    row = await _row()
    assert (row.host, row.moving_to) == ("rocky", None)
    assert await _lease_holder() == "rocky"
    assert (await _audits())[-1]["outcome"] == "moved"


@pytest.mark.asyncio
async def test_preflight_reports_targets_dirty_files_and_tasks(fleet):
    fx = fleet
    fx.dobby.preflight = {"dirtyCount": 1, "dirtyPaths": ["a.py"], "repoPrivate": True,
                          "backgroundTasks": [{"kind": "monitor", "name": "Monitor",
                                               "description": "ci", "replayable": True}]}
    out = await sm.preflight(fx.f, SID, "rocky")
    assert out["source"] == "dobby"
    assert out["hosts"] == [{"name": "rocky", "ok": True, "reason": None, "message": ""}]
    assert out["dirty"] == {"count": 1, "paths": ["a.py"]} and out["repoPrivate"] is True
    assert out["backgroundTasks"][0]["description"] == "ci"
    assert out["verdict"]["ok"] is True
    fx.dobby.preflight["repoPrivate"] = None
    out = await sm.preflight(fx.f, SID, "rocky")
    assert out["verdict"]["reason"] == sm.MOVE_DIRTY_PUBLIC_REPO


@pytest.mark.asyncio
async def test_live_steering_children_refuse_but_forks_do_not(fleet):
    fx = fleet
    async with db_session() as db:
        db.add(DbSession(id="fork-1", name="f", account_name="acct", status="done",
                         cwd="/x", parent_session_id=SID, is_fork=True))
        await db.commit()
    out = await sm.preflight(fx.f, SID, "rocky")
    assert out["verdict"]["ok"] is True
    async with db_session() as db:
        db.add(DbSession(id="child-1", name="c", account_name="acct", status="done",
                         cwd="/y", parent_session_id=SID))
        await db.commit()
    with pytest.raises(sm.MoveError) as ei:
        await _move(fx)
    assert ei.value.code == sm.MOVE_HAS_CHILDREN


@pytest.mark.asyncio
async def test_no_fleet_is_refused():
    with pytest.raises(sm.MoveError) as ei:
        await sm.move_session(object(), SID, "rocky", identity="tony@example.com")
    assert ei.value.code == sm.MOVE_NO_FLEET


# ═════════════════════════════════════════════════════════════════════════════════════
# Crash mid-saga: the reconcile settles stale claims by where the lease and tree are
# ═════════════════════════════════════════════════════════════════════════════════════

async def _stale_claim(*, host: str, moved_from: str | None = None, age_s: float = 900) -> None:
    async with db_session() as db:
        await db.execute(update(DbSession).where(DbSession.id == SID).values(
            host=host, moved_from=moved_from, moving_to="rocky",
            move_started_at=naive_utc_now() - timedelta(seconds=age_s)))
        await db.commit()


@pytest.mark.asyncio
async def test_reconcile_crash_after_export_restores_the_source(fleet):
    fx = fleet
    await fx.dobby.transfer_export(SID, target="rocky")      # the edge died right here
    await _stale_claim(host="dobby")
    assert await _lease_holder() is None
    assert await sm.reconcile_stale_moves(fx.f) == [SID]
    row = await _row()
    assert (row.host, row.moving_to) == ("dobby", None)
    assert await _lease_holder() == "dobby"
    _on_source_only(fx)
    assert fx.origin == set()
    assert (await _audits())[-1]["outcome"] == "reconciled:dobby"


@pytest.mark.asyncio
async def test_reconcile_crash_after_import_keeps_the_target(fleet):
    fx = fleet
    manifest = await fx.dobby.transfer_export(SID, target="rocky")
    await _stale_claim(host="rocky", moved_from="dobby")
    await fx.rocky.transfer_import(SID, manifest, source="dobby")   # target running it
    assert await sm.reconcile_stale_moves(fx.f) == [SID]
    row = await _row()
    assert (row.host, row.moving_to, row.moved_from) == ("rocky", None, "dobby")
    assert await _lease_holder() == "rocky"
    assert SID not in fx.dobby.trees and SID in fx.rocky.trees     # source finalized
    assert "restore" not in fx.rocky.calls                          # already live there


@pytest.mark.asyncio
async def test_reconcile_crash_after_restamp_before_import_restores_the_source(fleet):
    fx = fleet
    await fx.dobby.transfer_export(SID, target="rocky")
    await _stale_claim(host="rocky", moved_from="dobby")
    assert await sm.reconcile_stale_moves(fx.f) == [SID]
    row = await _row()
    assert (row.host, row.moving_to) == ("dobby", None)
    assert await _lease_holder() == "dobby"
    _on_source_only(fx)


@pytest.mark.asyncio
async def test_reconcile_target_died_mid_import_keeps_the_source(fleet):
    """Fix round 1, item 4: the target got as far as `worktree add` and died, then the
    edge died. A directory on the target is not evidence of a finished import: the
    source wins, resumes FIRST, and only then is the target's half copy dropped."""
    fx = fleet
    await fx.dobby.transfer_export(SID, target="rocky")
    await _stale_claim(host="rocky", moved_from="dobby")
    fx.rocky.trees.add(SID)                                  # the half-finished import
    fx.rocky.preflight = {"present": True}
    assert await sm.reconcile_stale_moves(fx.f) == [SID]
    row = await _row()
    assert (row.host, row.moving_to) == ("dobby", None)
    assert SID in fx.dobby.trees and SID not in fx.rocky.trees
    assert fx.dobby.events[-2:] == [("dobby", "restore"), ("rocky", "finalize")]
    _on_source_only(fx)


@pytest.mark.asyncio
async def test_reconcile_falls_back_when_the_winner_cannot_resume(fleet):
    """The import completed (marker) but the target cannot resume now: fall back to
    the source, never finalizing the source first."""
    fx = fleet
    manifest = await fx.dobby.transfer_export(SID, target="rocky")
    await _stale_claim(host="rocky", moved_from="dobby")
    await fx.rocky.transfer_import(SID, manifest, source="dobby")
    await _release(SID, "rocky")                              # its driver went idle
    fx.rocky.drivers.discard(SID)
    fx.dobby.trees.add(SID)                                   # source tree still there
    fx.rocky.fail["restore"] = RuntimeError("cannot resume")
    assert await sm.reconcile_stale_moves(fx.f) == [SID]
    row = await _row()
    assert (row.host, row.moving_to) == ("dobby", None)
    assert ("dobby", "finalize") not in fx.dobby.events
    assert fx.dobby.events[-2:] == [("dobby", "restore"), ("rocky", "finalize")]


@pytest.mark.asyncio
async def test_reconcile_leaves_fresh_and_live_claims_alone(fleet):
    fx = fleet
    await _stale_claim(host="dobby", age_s=0)    # claimed while this edge runs
    assert await sm.reconcile_stale_moves(fx.f) == []
    await _stale_claim(host="dobby", age_s=900)
    fx.f.begin_move(SID, "rocky")                     # a live saga in this edge
    assert await sm.reconcile_stale_moves(fx.f) == []
    fx.f.end_move(SID)
    assert await sm.reconcile_stale_moves(fx.f) == [SID]


# ═════════════════════════════════════════════════════════════════════════════════════
# Route cache self-heal (§C.3 gap 2)
# ═════════════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_route_cache_heals_when_the_stamp_moved(fleet):
    fx = fleet
    fx.rocky.records[SID] = fx.dobby.records.pop(SID)
    fx.f.note_host(SID, "dobby")                      # stale: restamped behind our back
    async with db_session() as db:
        await db.execute(update(DbSession).where(DbSession.id == SID).values(host="rocky"))
        await db.commit()
    rec = await fx.f.get_session(SID)
    assert rec is fx.rocky.records[SID]
    assert fx.f._host_cache[SID] == "rocky"


@pytest.mark.asyncio
async def test_route_cache_heals_on_a_no_such_session_error(fleet):
    fx = fleet
    fx.f.note_host(SID, "dobby")

    async def gone(sid):
        raise KeyError(f"No session with id {sid}")

    async def here(sid):
        return "rocky's answer"
    fx.dobby.transcript_path = gone
    fx.rocky.transcript_path = here
    async with db_session() as db:
        await db.execute(update(DbSession).where(DbSession.id == SID).values(host="rocky"))
        await db.commit()
    assert await fx.f.transcript_path(SID) == "rocky's answer"
    assert fx.f._host_cache[SID] == "rocky"


# ═════════════════════════════════════════════════════════════════════════════════════
# The daemon halves (transfer_*) against real git and a local bare origin
# ═════════════════════════════════════════════════════════════════════════════════════

def _git(*args: str, cwd) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def _origin_and_clone(root: Path, name: str) -> Path:
    origin = root / "origin.git"
    if not origin.exists():
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
        seed = root / "_seed"
        subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
        _git("config", "user.email", "t@example.com", cwd=seed)
        _git("config", "user.name", "T", cwd=seed)
        (seed / "README.md").write_text("hi\n")
        _git("add", "-A", cwd=seed)
        _git("commit", "-q", "-m", "init", cwd=seed)
        _git("remote", "add", "origin", str(origin), cwd=seed)
        _git("push", "-q", "-u", "origin", "main", cwd=seed)
    clone = root / name
    clone.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
    _git("config", "user.email", "t@example.com", cwd=clone)
    _git("config", "user.name", "T", cwd=clone)
    return clone


class _Driver:
    def __init__(self, *, turn_active=False):
        self.turn_active = turn_active
        self.closed = False

    async def aclose(self):
        self.closed = True


def _bare(monkeypatch, *, host: str, role: str, root: Path) -> Any:
    """A SessionOrchestrator built the way the lease-layer tests build theirs, with the
    move's collaborators either real (DB, lease, git, record mapper) or narrowed to a
    seam (driver teardown, ensure_driver, transcript location, background scan)."""
    import server.sessions.orchestrator as mod
    monkeypatch.setattr(mod, "daemon_role", lambda: role)
    monkeypatch.setattr(mod, "self_host_name", lambda: host)
    o: Any = SessionOrchestrator.__new__(SessionOrchestrator)
    o._records, o._drivers, o._swapping, o._spawning = {}, {}, {}, {}
    o._pending_prompt_preambles, o._worktrees, o._resume_recovery = {}, {}, {}
    o._rearmed, o._releasing = set(), {}
    o._repo_root = str(root)
    o._enabled_host_names = AsyncMock(return_value=BOTH)
    o._effective_repo_root = AsyncMock(return_value=str(root))
    o._open_background_work = AsyncMock(return_value=[])
    o._expire_rewinds = AsyncMock()
    o.closed = []

    async def close(sid):
        d = o._drivers.pop(sid, None)
        await o._release_session_lease(sid)
        if d is not None:
            await d.aclose()
            o.closed.append(sid)
    o._close_for_transfer = close
    return o


def _rec(sid, cwd, host, status="done") -> SessionRecord:
    return SessionRecord(id=sid, name="s", account="acct", engine=AgentEngine.CLAUDE_CODE,
                         engine_session_id="c-1", cols=80, rows=24, cwd=str(cwd),
                         detached=False, status=status, host=host, repo="acme/myrepo")


async def _seed_row(cwd, host, **kw) -> None:
    async with db_session() as db:
        db.add(DbSession(id=SID, name="s", account_name="acct", engine_session_id="c-1",
                         status=kw.pop("status", "done"), cwd=str(cwd), host=host,
                         repo="acme/myrepo", **kw))
        await db.commit()


@pytest.fixture
def two_hosts(tmp_path, isolated_db):
    """hostA (source) and hostB (target) clones of one bare origin, and the session's
    worktree on hostA. In production the two paths are byte-identical; here each host
    has its own repo root, so the target's row cwd is set to hostB's canonical path."""
    root = tmp_path
    a_root, b_root = root / "hostA", root / "hostB"
    a_root.mkdir()
    b_root.mkdir()
    a_clone = _origin_and_clone(root, "hostA/myrepo")
    b_clone = _origin_and_clone(root, "hostB/myrepo")
    info = worktrees.create_worktree(str(a_clone), "main", SID, str(a_root))
    transcript = root / "projects" / "c-1.jsonl"
    _write_transcript(transcript, ["u1", "u2"])
    return SimpleNamespace(root=root, a_root=a_root, b_root=b_root, a_clone=a_clone,
                           b_clone=b_clone, cwd=Path(info.path),
                           b_cwd=worktrees.worktrees_base(str(b_root)) / "myrepo" / "5e55a0aa",
                           origin=root / "origin.git", transcript=transcript)



def _origin_refs(origin: Path) -> str:
    return subprocess.run(["git", "-C", str(origin), "for-each-ref", wt.TRANSFER_REF_NS + "/"],
                          capture_output=True, text=True).stdout.strip()


class _Calls(list):
    answer: bool | None


@pytest.fixture
def visibility(monkeypatch):
    calls = _Calls()
    calls.answer = True
    monkeypatch.setattr(gitrepos, "repo_is_private", lambda repo: calls.answer)
    return calls


async def _source(monkeypatch, h, *, role="primary"):
    o = _bare(monkeypatch, host="dobby", role=role, root=h.a_root)
    o._records[SID] = _rec(SID, h.cwd, "dobby")
    o._drivers[SID] = _Driver()
    o.transcript_path = lambda sid: str(h.transcript)
    await _seed_row(h.cwd, "dobby")
    assert await o._claim_session(SID, why="test") is True
    return o


@pytest.mark.asyncio
async def test_export_refuses_a_dirty_tree_without_consent_and_touches_nothing(
        monkeypatch, two_hosts, visibility):
    h = two_hosts
    o = await _source(monkeypatch, h)
    (h.cwd / "wip.py").write_text("x\n")
    with pytest.raises(sm.MoveError) as ei:
        await o.transfer_export(SID, target="rocky")
    assert ei.value.code == sm.MOVE_DIRTY_NEEDS_CONSENT
    assert ei.value.extra == {"dirtyCount": 1, "dirtyPaths": ["wip.py"]}
    assert SID in o._drivers and o.closed == []            # nothing touched
    assert o._records[SID].host == "dobby"
    assert _origin_refs(h.origin) == ""
    assert SID not in o._swapping                          # slot released


@pytest.mark.parametrize("answer", [False, None])
@pytest.mark.asyncio
async def test_export_refuses_dirty_on_a_public_or_unknown_repo_even_with_consent(
        monkeypatch, two_hosts, visibility, answer):
    h = two_hosts
    visibility.answer = answer
    o = await _source(monkeypatch, h)
    (h.cwd / "secret.env").write_text("TOKEN=x\n")
    with pytest.raises(sm.MoveError) as ei:
        await o.transfer_export(SID, target="rocky", carry_dirty=True)
    assert ei.value.code == sm.MOVE_DIRTY_PUBLIC_REPO
    assert _origin_refs(h.origin) == "" and o.closed == []


def _commit_in(cwd: Path, name: str = "feature.py") -> None:
    (cwd / name).write_text("x = 1\n")
    _git("add", "-A", cwd=cwd)
    _git("commit", "-q", "-m", name, cwd=cwd)


@pytest.mark.parametrize("answer", [False, None])
@pytest.mark.asyncio
async def test_export_refuses_unpushed_commits_on_a_public_or_unknown_repo(
        monkeypatch, two_hosts, visibility, answer):
    h = two_hosts
    visibility.answer = answer
    o = await _source(monkeypatch, h)
    _commit_in(h.cwd)
    with pytest.raises(sm.MoveError) as ei:
        await o.transfer_export(SID, target="rocky")
    assert ei.value.code == sm.MOVE_UNPUSHED_PUBLIC_REPO
    assert ei.value.extra == {"unpushedCommits": 1}
    assert _origin_refs(h.origin) == "" and o.closed == []     # nothing touched
    assert SID in o._drivers and o._records[SID].host == "dobby"


@pytest.mark.asyncio
async def test_export_carries_unpushed_commits_on_a_private_repo(
        monkeypatch, two_hosts, visibility):
    h = two_hosts
    o = await _source(monkeypatch, h)
    _commit_in(h.cwd)
    manifest = await o.transfer_export(SID, target="rocky")
    assert manifest["bundle"]["headSha"] == _git("rev-parse", "HEAD", cwd=h.cwd)
    assert _origin_refs(h.origin) != ""


@pytest.mark.parametrize("answer,dirty", [(False, False), (None, True)])
@pytest.mark.asyncio
async def test_preflight_reports_unpushed_commits_and_visibility(
        monkeypatch, two_hosts, visibility, answer, dirty):
    h = two_hosts
    visibility.answer = answer
    o = await _source(monkeypatch, h)
    o.orphaned_interaction = lambda sid: None
    _commit_in(h.cwd)
    if dirty:
        (h.cwd / "wip.py").write_text("y\n")
    report = await o.transfer_preflight(SID)
    assert report["unpushedCommits"] == 1
    assert report["repoPrivate"] is answer
    facts = sm.WorktreeFacts.from_preflight(report)
    assert facts.unpushed_commits == 1


@pytest.mark.asyncio
async def test_export_refuses_mid_turn_inside_the_slot(monkeypatch, two_hosts, visibility):
    h = two_hosts
    o = await _source(monkeypatch, h)
    o._drivers[SID] = _Driver(turn_active=True)
    with pytest.raises(sm.MoveError) as ei:
        await o.transfer_export(SID, target="rocky")
    assert ei.value.code == sm.MOVE_MID_TURN and o.closed == []


@pytest.mark.asyncio
async def test_export_lists_background_tasks_and_snapshots_them_for_rearm(
        monkeypatch, two_hosts, visibility):
    from server.sessions.background_tasks import OpenBgTask
    h = two_hosts
    o = await _source(monkeypatch, h)
    task = OpenBgTask(tool_use_id="t1", kind="monitor", name="Monitor",
                      input={"command": "watch"}, description="watch CI")
    o._open_background_work = AsyncMock(return_value=[task])
    with pytest.raises(sm.MoveError) as ei:
        await o.transfer_export(SID, target="rocky")
    assert ei.value.code == sm.MOVE_BACKGROUND_NEEDS_CONFIRM
    assert ei.value.extra["backgroundTasks"][0]["description"] == "watch CI"
    manifest = await o.transfer_export(SID, target="rocky", stop_background=True)
    assert manifest["backgroundTasks"][0]["name"] == "Monitor"
    row = await _row()
    assert row.pending_rearm == [task.to_dict()]           # the target re-arms these


@pytest.mark.asyncio
async def test_export_closes_pushes_and_retargets_the_primary_mirror(
        monkeypatch, two_hosts, visibility):
    h = two_hosts
    o = await _source(monkeypatch, h)
    o._pending_prompt_preambles[SID] = ["a note for the next prompt"]
    (h.cwd / "wip.py").write_text("x\n")
    manifest = await o.transfer_export(SID, target="rocky", carry_dirty=True)
    assert o.closed == [SID] and SID not in o._drivers
    assert await _lease_holder() is None                  # released
    assert o._records[SID].host == "rocky"                 # gap 1: mirror retargeted
    assert manifest["bundle"]["wipCommit"] is True
    assert manifest["transcript"] == sm.transcript_marker(str(h.transcript))
    assert manifest["preambles"] == ["a note for the next prompt"]
    assert wt.head_ref(SID) in _origin_refs(h.origin)
    o._expire_rewinds.assert_awaited_once()
    assert SID not in o._swapping


@pytest.mark.asyncio
async def test_export_on_a_worker_forgets_the_record(monkeypatch, two_hosts, visibility):
    h = two_hosts
    o = await _source(monkeypatch, h, role="worker")
    await o.transfer_export(SID, target="dobby-2")
    assert SID not in o._records


@pytest.mark.asyncio
async def test_export_push_failure_keeps_the_record_and_the_local_restore_recovers(
        monkeypatch, two_hosts, visibility):
    h = two_hosts
    o = await _source(monkeypatch, h)
    (h.cwd / "wip.py").write_text("x\n")
    base = _git("rev-parse", "HEAD", cwd=h.cwd)
    _git("remote", "set-url", "origin", str(h.root / "nowhere.git"), cwd=h.a_clone)
    with pytest.raises(sm.MoveError) as ei:
        await o.transfer_export(SID, target="rocky", carry_dirty=True)
    assert ei.value.code == sm.MOVE_EXPORT_FAILED
    assert o._records[SID].host == "dobby"                 # never retargeted
    assert _git("rev-parse", "HEAD", cwd=h.cwd) == base    # WIP undone by export
    o.ensure_driver = AsyncMock(side_effect=lambda sid: o._drivers.setdefault(sid, _Driver()))
    out = await o.transfer_import(SID, None, source="rocky", local=True)
    assert out["host"] == "dobby" and SID in o._drivers


@pytest.mark.asyncio
async def test_local_restore_unpacks_the_wip_and_deletes_the_refs(
        monkeypatch, two_hosts, visibility):
    """Restore-to-source after a successful export (import failed on the target)."""
    h = two_hosts
    o = await _source(monkeypatch, h, role="worker")
    (h.cwd / "wip.py").write_text("x\n")
    base = _git("rev-parse", "HEAD", cwd=h.cwd)
    manifest = await o.transfer_export(SID, target="rocky", carry_dirty=True)
    assert SID not in o._records
    o.ensure_driver = AsyncMock(side_effect=lambda sid: o._drivers.setdefault(sid, _Driver()))
    await o.transfer_import(SID, manifest, source="rocky", local=True)
    assert _git("rev-parse", "HEAD", cwd=h.cwd) == base   # branch tip never keeps the WIP
    assert (h.cwd / "wip.py").read_text() == "x\n"
    assert o._records[SID].host == "dobby"
    assert await _lease_holder() == "dobby"
    assert _origin_refs(h.origin) == ""


async def _target(monkeypatch, h, *, role="worker"):
    o = _bare(monkeypatch, host="rocky", role=role, root=h.b_root)
    o._clone_for_move = AsyncMock(return_value=str(h.b_clone))
    o.transcript_path = lambda sid: str(h.transcript)

    async def ensure(sid, *a, **k):
        assert o._records[sid].host == "rocky"             # layer 4: stamped before driver
        assert await o._claim_session(sid, why="resume")
        return o._drivers.setdefault(sid, _Driver())
    o.ensure_driver = ensure
    await _seed_row(h.b_cwd, "rocky", moving_to="rocky")
    return o


def _manifest(h, *, carry=False) -> dict:
    b = wt.export(str(h.cwd), SID, carry_dirty=carry, vetted=wt.inspect(str(h.cwd)))
    return {"sessionId": SID, "bundle": b.to_dict(), "originUrl": str(h.origin),
            "transcript": sm.transcript_marker(str(h.transcript)),
            "preambles": ["carried note"], "cwd": str(h.b_cwd)}


@pytest.mark.asyncio
async def test_import_recreates_the_tree_resumes_and_deletes_the_refs(monkeypatch, two_hosts):
    h = two_hosts
    (h.cwd / "wip.py").write_text("x\n")
    manifest = _manifest(h, carry=True)
    o = await _target(monkeypatch, h)
    out = await o.transfer_import(SID, manifest, source="dobby")
    assert out == {"sessionId": SID, "host": "rocky", "driver": True}
    assert (h.b_cwd / "wip.py").read_text() == "x\n"      # uncommitted change arrived
    assert _git("rev-parse", "HEAD", cwd=h.b_cwd) == manifest["bundle"]["baseSha"]
    assert o._records[SID].host == "rocky"                 # gap 2: loaded from the row
    assert await _lease_holder() == "rocky"
    assert _origin_refs(h.origin) == ""                    # D1: ref deleted after import
    pre = o._pending_prompt_preambles[SID]
    assert pre[0] == "carried note"
    assert pre[1] == ("This session moved from dobby to rocky; the worktree and "
                      "uncommitted changes came with it.")


@pytest.mark.asyncio
async def test_import_refuses_a_row_not_yet_restamped(monkeypatch, two_hosts):
    h = two_hosts
    manifest = _manifest(h)
    o = await _target(monkeypatch, h)
    async with db_session() as db:
        await db.execute(update(DbSession).where(DbSession.id == SID).values(host="dobby"))
        await db.commit()
    with pytest.raises(sm.MoveError) as ei:
        await o.transfer_import(SID, manifest, source="dobby")
    assert ei.value.code == sm.MOVE_NOT_STAMPED
    assert not h.b_cwd.exists()


@pytest.mark.asyncio
async def test_import_never_resumes_a_stale_transcript(monkeypatch, two_hosts):
    h = two_hosts
    manifest = _manifest(h)
    manifest["transcript"] = {"size": 10 ** 6, "lastUuid": "u-the-source-wrote-last"}
    monkeypatch.setattr(sm, "TRANSCRIPT_RETRY_S", 0.0)
    o = await _target(monkeypatch, h)
    resumed = []
    o.ensure_driver = AsyncMock(side_effect=lambda sid: resumed.append(sid))
    with pytest.raises(sm.MoveError) as ei:
        await o.transfer_import(SID, manifest, source="dobby")
    assert ei.value.code == sm.MOVE_TRANSCRIPT_STALE
    assert resumed == []                                   # never resumed
    assert not h.b_cwd.exists()                            # its imported tree is gone
    assert SID not in o._records and SID not in o._drivers
    assert await _lease_holder() is None


@pytest.mark.asyncio
async def test_import_waits_out_a_briefly_stale_transcript(monkeypatch, two_hosts):
    """rocky's sshfs cache: the first read misses the last turn, a re-read sees it."""
    h = two_hosts
    manifest = _manifest(h)
    full = sm.transcript_marker(str(h.transcript))
    _write_transcript(h.transcript, ["u1"])              # what the target sees first
    manifest["transcript"] = full
    monkeypatch.setattr(sm, "TRANSCRIPT_RETRY_S", 0.0)
    o = await _target(monkeypatch, h)
    seen = []

    def path(sid):
        seen.append(1)
        if len(seen) == 2:
            _write_transcript(h.transcript, ["u1", "u2"])
        return str(h.transcript)
    o.transcript_path = path
    await o.transfer_import(SID, manifest, source="dobby")
    assert len(seen) == 2 and SID in o._drivers


@pytest.mark.asyncio
async def test_import_resume_failure_undoes_everything_on_the_target(monkeypatch, two_hosts):
    h = two_hosts
    manifest = _manifest(h)
    o = await _target(monkeypatch, h, role="primary")
    o.ensure_driver = AsyncMock(return_value=None)
    with pytest.raises(sm.MoveError) as ei:
        await o.transfer_import(SID, manifest, source="dobby")
    assert ei.value.code == sm.MOVE_RESUME_FAILED
    assert not h.b_cwd.exists()
    assert o._records[SID].host == "dobby"                 # primary mirror points back
    assert await _lease_holder() is None


@pytest.mark.asyncio
async def test_finalize_snapshots_then_removes_the_source_tree(
        monkeypatch, two_hosts, visibility):
    h = two_hosts
    o = await _source(monkeypatch, h)
    (h.cwd / "wip.py").write_text("precious\n")
    manifest = await o.transfer_export(SID, target="rocky", carry_dirty=True)
    with pytest.raises(sm.MoveError) as ei:            # the row still says dobby
        await o.transfer_finalize(SID, manifest, owner="rocky")
    assert ei.value.code == sm.MOVE_FINALIZE_REFUSED and h.cwd.exists()
    async with db_session() as db:
        await db.execute(update(DbSession).where(DbSession.id == SID).values(host="rocky"))
        await db.commit()
    out = await o.transfer_finalize(SID, manifest, owner="rocky")
    assert out["removed"] is True and not h.cwd.exists()
    ref = out["recoveryRef"]
    assert ref and ref in worktrees.recovery_refs(str(h.a_clone), SID)
    assert _git("show", f"{ref}:wip.py", cwd=h.a_clone) == "precious"
    assert o._records[SID].host == "rocky"


# ═════════════════════════════════════════════════════════════════════════════════════
# Split-brain (§C.3): no twin resume after a move
# ═════════════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_primary_stale_mirror_cannot_resume_after_a_move(monkeypatch, isolated_db,
                                                               tmp_path):
    """dobby → rocky moved: the primary's mirror says rocky, the row says rocky, so no
    primary path (stuck sweep, rotation, a mis-routed ensure_driver) can claim it —
    the leftover source tree is never resumed (Review Focus 3)."""
    o = _bare(monkeypatch, host="dobby", role="primary", root=tmp_path)
    o._records[SID] = _rec(SID, tmp_path / "wt", "rocky")
    await _seed_row(tmp_path / "wt", "rocky", moved_from="dobby")
    assert await o._claim_session(SID, why="resume") is False
    assert await _lease_holder() is None


@pytest.mark.asyncio
async def test_primary_mirror_mid_move_is_not_healed(monkeypatch, isolated_db, tmp_path):
    """Between export and restamp the row still names dobby, but a move is in flight:
    the in-memory retarget holds, nothing heals it back."""
    o = _bare(monkeypatch, host="dobby", role="primary", root=tmp_path)
    o._records[SID] = _rec(SID, tmp_path / "wt", "rocky")
    await _seed_row(tmp_path / "wt", "dobby", moving_to="rocky")
    assert await o._claim_session(SID, why="resume") is False
    assert o._records[SID].host == "rocky"


@pytest.mark.asyncio
async def test_primary_mirror_heals_when_the_row_came_back(monkeypatch, isolated_db, tmp_path):
    """A failed move restored to dobby while the mirror still said rocky: the row is the
    authority and no move is in flight, so the claim proceeds."""
    o = _bare(monkeypatch, host="dobby", role="primary", root=tmp_path)
    o._records[SID] = _rec(SID, tmp_path / "wt", "rocky")
    await _seed_row(tmp_path / "wt", "dobby")
    assert await o._claim_session(SID, why="resume") is True
    assert o._records[SID].host == "dobby"


@pytest.mark.asyncio
async def test_worker_ensure_driver_loads_a_row_it_never_rehydrated(monkeypatch, isolated_db,
                                                                    tmp_path):
    o = _bare(monkeypatch, host="rocky", role="worker", root=tmp_path)
    o._ensure_lock = asyncio.Lock()
    await _seed_row(tmp_path / "wt", "rocky")
    claimed = []

    async def claim(sid, *, why, adopt=False):
        claimed.append(sid)
        return False                                   # stop before a real build
    o._claim_session = claim
    assert await o.ensure_driver(SID) is None
    assert claimed == [SID]                            # got past "unknown id → None"
    assert o._records[SID].host == "rocky"


@pytest.mark.parametrize("row_kw", [{"host": "dobby"}, {"host": "rocky", "moving_to": "x"},
                                    {"host": "rocky", "status": "archived"}])
@pytest.mark.asyncio
async def test_worker_does_not_load_rows_that_are_not_its_own(monkeypatch, isolated_db,
                                                              tmp_path, row_kw):
    o = _bare(monkeypatch, host="rocky", role="worker", root=tmp_path)
    host = row_kw.pop("host")
    await _seed_row(tmp_path / "wt", host, **row_kw)
    assert await o._load_unrehydrated_row(SID) is None
    assert SID not in o._records


# ═════════════════════════════════════════════════════════════════════════════════════
# Security: a person only; the route shape
# ═════════════════════════════════════════════════════════════════════════════════════

def _app(sessions, *, identity="tony@example.com", via="local-trusted") -> TestClient:
    from server.api.sessions import router
    app = FastAPI()
    app.include_router(router)
    app.state.sessions = sessions

    @app.middleware("http")
    async def _ident(request, call_next):
        request.state.identity = identity
        request.state.via = via
        return await call_next(request)
    return TestClient(app)


@pytest.mark.parametrize("identity,via", [
    ("assistant@viberpit", "assistant"), ("cli@viberpit", "cli"), ("system", None)])
def test_move_is_human_only(identity, via):
    from server.sessions.session_actions import SESSION_CONTROL_HUMAN_ONLY_CODE
    with _app(object(), identity=identity, via=via) as c:
        r = c.post(f"/api/sessions/{SID}/move", json={"host": "rocky"})
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == SESSION_CONTROL_HUMAN_ONLY_CODE


def test_move_route_maps_refusals(isolated_db):
    with _app(object()) as c:
        r = c.post(f"/api/sessions/{SID}/move", json={"host": "rocky"})
        assert r.status_code == 409 and r.json()["detail"]["code"] == sm.MOVE_NO_FLEET
        r = c.get(f"/api/sessions/{SID}/move/preflight?host=rocky")
        assert r.status_code == 409 and r.json()["detail"]["code"] == sm.MOVE_NO_FLEET


def test_move_error_round_trips_the_rpc_envelope():
    exc = sm.MoveError(sm.MOVE_DIRTY_NEEDS_CONSENT, "dirty",
                       extra={"dirtyPaths": ["a"]}, outcome=None)
    back = sm.MoveError.from_error_data("dirty", exc.error_data)
    assert (back.code, back.extra, back.outcome) == (sm.MOVE_DIRTY_NEEDS_CONSENT,
                                                    {"dirtyPaths": ["a"]}, None)
    failed = sm.MoveError(sm.MOVE_IMPORT_FAILED, outcome="restored")
    assert sm.MoveError.from_error_data("x", failed.error_data).outcome == "restored"



# ═════════════════════════════════════════════════════════════════════════════════════
# Fix round 1
# ═════════════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_a_host_missing_from_the_router_is_a_coded_refusal(fleet, monkeypatch):
    """Item 8: the registry lists rocky but the router holds no client for it."""
    fx = fleet

    async def cands():
        return [_cand("rocky")]
    monkeypatch.setattr(fx.f, "host_candidates", cands)
    del fx.f._router._orchs["rocky"]
    monkeypatch.setattr(fx.f, "host_names", lambda: ["dobby", "rocky"])
    with pytest.raises(sm.MoveError) as ei:
        await _move(fx)
    assert ei.value.code == sm.MOVE_TARGET_UNAVAILABLE
    row = await _row()
    assert (row.host, row.moving_to) == ("dobby", None)
    assert "export" not in fx.dobby.calls


def test_a_resting_status_with_no_live_card_is_movable():
    """Item 10: a seeded waiting_question after a restart has no card behind it."""
    assert _verdict(_subject(status="waiting_question")).ok
    assert _verdict(_subject(status="waiting_input"),
                    facts=sm.WorktreeFacts()).ok
    v = _verdict(facts=sm.WorktreeFacts(pending_card=True))
    assert v.code == sm.MOVE_AWAITING_ANSWER


def test_preflight_is_human_only():
    """Item 9: the dialog's preflight reveals dirty file names — same gate as move."""
    from server.sessions.session_actions import SESSION_CONTROL_HUMAN_ONLY_CODE
    with _app(object(), identity="assistant@viberpit", via="assistant") as c:
        r = c.get(f"/api/sessions/{SID}/move/preflight?host=rocky")
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == SESSION_CONTROL_HUMAN_ONLY_CODE


@pytest.mark.asyncio
async def test_export_never_pushes_a_tree_dirtied_after_vetting(
        monkeypatch, two_hosts, visibility):
    """Item 1: clean when vetted, dirty by the time export runs (a background build),
    public repo, consent requested anyway — nothing is pushed."""
    h = two_hosts
    visibility.answer = False
    o = await _source(monkeypatch, h)
    real_close = o._close_for_transfer

    async def close_then_dirty(sid):
        await real_close(sid)
        (h.cwd / "build.log").write_text("TOKEN=late\n")
    o._close_for_transfer = close_then_dirty
    with pytest.raises(sm.MoveError) as ei:
        await o.transfer_export(SID, target="rocky", carry_dirty=True)
    assert ei.value.code == sm.MOVE_EXPORT_FAILED
    assert _origin_refs(h.origin) == ""
    assert o._records[SID].host == "dobby"


@pytest.mark.asyncio
async def test_a_lost_restore_claim_gives_the_record_to_the_lease_holder(
        monkeypatch, two_hosts, visibility):
    """Item 2 (daemon half): the local restore installed a record before its claim
    lost to the target; the record must not stay stamped here."""
    h = two_hosts
    o = await _source(monkeypatch, h)
    manifest = await o.transfer_export(SID, target="rocky")
    assert await session_lease.acquire(SID, "rocky", ttl_s=120) is not None
    o.ensure_driver = AsyncMock(side_effect=AssertionError("must not resume"))
    with pytest.raises(sm.MoveError) as ei:
        await o.transfer_import(SID, manifest, source="rocky", local=True)
    assert ei.value.code == sm.MOVE_LEASE_HELD
    assert o._records[SID].host == "rocky"
    assert await o._claim_session(SID, why="resume") is False      # no twin here


@pytest.mark.asyncio
async def test_ensure_driver_unpacks_a_stray_transfer_wip(monkeypatch, two_hosts):
    """Item 3 defence in depth: a session about to resume on its own transfer WIP
    commit gets it unpacked first, so the agent's next push cannot publish it."""
    h = two_hosts
    o = _bare(monkeypatch, host="dobby", role="primary", root=h.a_root)
    o._ensure_lock = asyncio.Lock()
    base = _git("rev-parse", "HEAD", cwd=h.cwd)
    (h.cwd / "wip.py").write_text("x\n")
    wt.export(str(h.cwd), SID, carry_dirty=True, vetted=wt.inspect(str(h.cwd)))
    assert _git("rev-parse", "HEAD", cwd=h.cwd) != base
    o._records[SID] = _rec(SID, h.cwd, "dobby")
    o._claim_session = AsyncMock(return_value=True)
    o._ensure_session_cwd = AsyncMock(return_value=True)

    class _Stop(Exception):
        pass

    async def stop(**kw):
        raise _Stop
    import server.sessions.orchestrator as mod
    monkeypatch.setattr(mod, "disabled_for_session", stop)
    with pytest.raises(_Stop):
        await o.ensure_driver(SID)
    assert _git("rev-parse", "HEAD", cwd=h.cwd) == base
    assert (h.cwd / "wip.py").read_text() == "x\n"


@pytest.mark.asyncio
async def test_close_for_transfer_overrun_fails_the_export(monkeypatch, isolated_db, tmp_path):
    """Item 6: a CLI that does not close in time may still be flushing the shared
    transcript; the move must not go on."""
    import server.sessions.orchestrator as mod
    monkeypatch.setattr(mod, "DRIVER_RELEASE_WAIT_S", 0.05)
    o = _bare(monkeypatch, host="dobby", role="primary", root=tmp_path)
    del o._close_for_transfer                                   # the real one

    class _Stuck:
        async def aclose(self):
            await asyncio.sleep(30)
    o._teardown_driver_state = AsyncMock(return_value=_Stuck())
    with pytest.raises(sm.MoveError) as ei:
        await o._close_for_transfer(SID)
    assert ei.value.code == sm.MOVE_EXPORT_FAILED


@pytest.mark.asyncio
async def test_a_session_being_closed_is_not_resurrected(monkeypatch, isolated_db, tmp_path):
    """Item 7: close_session pops the record before persisting `ended`."""
    o = _bare(monkeypatch, host="rocky", role="worker", root=tmp_path)
    await _seed_row(tmp_path / "wt", "rocky")
    o._closing = {SID}
    assert await o._load_unrehydrated_row(SID) is None
    assert SID not in o._records


@pytest.mark.asyncio
async def test_export_checks_the_live_card_not_the_resting_status(
        monkeypatch, two_hosts, visibility):
    """Item 10 (daemon half)."""
    h = two_hosts
    o = await _source(monkeypatch, h)
    o._records[SID].status = "waiting_question"
    o.orphaned_interaction = lambda sid: None
    drv = _Driver()
    drv.awaiting_user_input = True
    o._drivers[SID] = drv
    with pytest.raises(sm.MoveError) as ei:
        await o.transfer_export(SID, target="rocky")
    assert ei.value.code == sm.MOVE_AWAITING_ANSWER
    drv.awaiting_user_input = False
    manifest = await o.transfer_export(SID, target="rocky")
    assert manifest["target"] == "rocky"


# ═════════════════════════════════════════════════════════════════════════════════════
# Fix round 2
# ═════════════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_a_saga_ending_during_a_reconcile_scan_leaves_no_hold(fleet, monkeypatch):
    """A: the scan saw the claim while the saga ran; the saga finished (claim cleared,
    end_move) before the reconcile got to that row. Nothing may stay held."""
    fx = fleet
    fx.rocky.gate = asyncio.Event()
    saga = asyncio.create_task(_move(fx))
    while "import" not in fx.rocky.calls:
        await asyncio.sleep(0.01)
    scanned = await sm._scan_claims()                  # sees the live claim
    assert [r.id for r in scanned] == [SID]
    fx.rocky.gate.set()
    assert (await saga)["outcome"] == "moved"          # claim released, end_move done

    async def stale_scan():
        return scanned
    monkeypatch.setattr(sm, "_scan_claims", stale_scan)
    assert await sm.reconcile_stale_moves(fx.f) == []
    assert fx.f._held == {}
    assert (await fx.f.get_session(SID)).moving_to is None
    assert await fx.f.ensure_driver(SID) is not None   # drivable, not SESSION_MOVING


@pytest.mark.asyncio
async def test_a_hold_whose_claim_is_gone_is_released(fleet):
    """A: a hold left from an earlier pass is released once the DB has no claim."""
    fx = fleet
    fx.f.hold_move(SID, "rocky")
    assert await sm.reconcile_stale_moves(fx.f) == []
    assert fx.f._held == {}


@pytest.mark.asyncio
async def test_a_lease_without_the_import_marker_never_finalizes_the_source(
        fleet, monkeypatch):
    """B: the target claimed the lease (import in progress), the edge's import RPC timed
    out, then the target's ensure_driver failed and it abandoned its own tree. The
    source must not have been finalized on the lease alone: it survives and the session
    is restored there."""
    fx = fleet
    monkeypatch.setattr(sm, "IMPORT_SETTLE_S", 0.3, raising=False)
    monkeypatch.setattr(sm, "IMPORT_SETTLE_POLL_S", 0.02, raising=False)
    abandoned = asyncio.Event()

    async def import_then_fail(sid, manifest=None, *, source=None, local=False):
        fx.rocky.calls.append("restore" if local else "import")
        if local:
            raise sm.MoveError(sm.MOVE_WORKTREE_MISSING)
        assert await session_lease.acquire(sid, "rocky", ttl_s=120) is not None
        fx.rocky.trees.add(sid)

        async def ensure_driver_fails_later():
            await asyncio.sleep(0.1)
            fx.rocky.trees.discard(sid)                 # _abandon removes its tree
            await _release(sid, "rocky")
            abandoned.set()
        asyncio.get_running_loop().create_task(ensure_driver_fails_later())
        raise RuntimeError("DaemonTimeout: transfer_import")   # the edge gave up
    fx.rocky.transfer_import = import_then_fail
    with pytest.raises(sm.MoveError) as ei:
        await _move(fx)
    await abandoned.wait()
    assert "finalize" not in fx.dobby.calls
    assert SID in fx.dobby.trees                        # the only tree survives
    assert ei.value.outcome == "failed"                 # left to the reconcile
    assert await sm.reconcile_stale_moves(fx.f) == [SID]
    row = await _row()
    assert (row.host, row.moving_to) == ("dobby", None)
    _on_source_only(fx)
    assert await _lease_holder() == "dobby"


@pytest.mark.asyncio
async def test_reconcile_waits_for_a_target_holding_the_lease_without_the_marker(fleet):
    """B: in the reconcile too, a lease without the marker is an import in progress —
    never a winner, never a reason to finalize the source."""
    fx = fleet
    await fx.dobby.transfer_export(SID, target="rocky")
    await _stale_claim(host="rocky", moved_from="dobby")
    assert await session_lease.acquire(SID, "rocky", ttl_s=120) is not None
    fx.rocky.trees.add(SID)
    assert await sm.reconcile_stale_moves(fx.f) == []
    assert "finalize" not in fx.dobby.calls and SID in fx.dobby.trees
    assert (await _row()).moving_to == "rocky"
    with pytest.raises(sm.SessionMoving):
        await fx.f.ensure_driver(SID)


@pytest.mark.asyncio
async def test_a_stuck_reconcile_warns_once_per_episode(fleet, caplog):
    """C: neither host can resume — one WARNING for the episode, INFO after."""
    import logging
    fx = fleet
    await fx.dobby.transfer_export(SID, target="rocky")
    await _stale_claim(host="dobby")
    fx.dobby.fail["restore"] = RuntimeError("down")
    with caplog.at_level(logging.INFO, logger="server.sessions.session_move"):
        for _ in range(3):
            assert await sm.reconcile_stale_moves(fx.f) == []
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING
                and r.name == "server.sessions.session_move"]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    fx.dobby.fail.pop("restore")
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="server.sessions.session_move"):
        assert await sm.reconcile_stale_moves(fx.f) == [SID]
    await _stale_claim(host="dobby")                    # a NEW episode warns again
    fx.dobby.fail["restore"] = RuntimeError("down again")
    with caplog.at_level(logging.INFO, logger="server.sessions.session_move"):
        await sm.reconcile_stale_moves(fx.f)
    assert any(r.levelno >= logging.WARNING for r in caplog.records
               if r.name == "server.sessions.session_move")



# ═════════════════════════════════════════════════════════════════════════════════════
# Fix round 3
# ═════════════════════════════════════════════════════════════════════════════════════

async def _marker() -> str | None:
    return (await _row()).move_imported_by


@pytest.mark.asyncio
async def test_a_marker_write_that_raises_after_commit_keeps_the_target_tree(
        monkeypatch, two_hosts):
    """Item 1: the marker commit landed, then the context exit raised. Nothing after
    the marker may abandon: the marker says "live here", so the tree must stay."""
    h = two_hosts
    manifest = _manifest(h)
    o = await _target(monkeypatch, h)
    real = sm.mark_import_complete

    async def commit_then_raise(sid, host):
        await real(sid, host)
        raise RuntimeError("connection reset after COMMIT")
    monkeypatch.setattr(sm, "mark_import_complete", commit_then_raise)
    with pytest.raises(RuntimeError):
        await o.transfer_import(SID, manifest, source="dobby")
    assert await _marker() == "rocky"
    assert h.b_cwd.exists() and SID in o._drivers          # consistent with the marker


@pytest.mark.asyncio
async def test_abandon_clears_the_marker_before_touching_the_tree(monkeypatch, two_hosts):
    """Item 1: a marker left set from an earlier attempt must not survive the tree."""
    h = two_hosts
    manifest = _manifest(h)
    o = await _target(monkeypatch, h)
    async with db_session() as db:
        await db.execute(update(DbSession).where(DbSession.id == SID)
                         .values(move_imported_by="rocky"))
        await db.commit()
    o.ensure_driver = AsyncMock(return_value=None)
    with pytest.raises(sm.MoveError):
        await o.transfer_import(SID, manifest, source="dobby")
    assert await _marker() is None
    assert not h.b_cwd.exists()


async def _stale_rows():
    return await sm._scan_claims()


@pytest.mark.asyncio
async def test_reconcile_skips_a_claim_a_live_saga_took_after_the_scan(fleet, monkeypatch):
    """Item 2: the scan saw a stale claim; a saga in this edge started before the
    re-read. The reconcile must leave it to the saga."""
    fx = fleet
    await _stale_claim(host="dobby")
    rows = await _stale_rows()

    async def stale_scan():
        return rows
    real_hold = fx.f.hold_move

    def hold_then_saga_starts(sid, target):
        real_hold(sid, target)
        fx.f.begin_move(SID, "rocky")             # between the hold and the re-read
    monkeypatch.setattr(sm, "_scan_claims", stale_scan)
    monkeypatch.setattr(fx.f, "hold_move", hold_then_saga_starts)
    assert await sm.reconcile_stale_moves(fx.f) == []
    assert "restore" not in fx.dobby.calls


@pytest.mark.asyncio
async def test_reconcile_skips_a_different_saga_with_the_same_target(fleet, monkeypatch):
    """Item 2: same moving_to, but a different (newer) claim than the one scanned."""
    fx = fleet
    await _stale_claim(host="dobby")
    rows = await _stale_rows()
    await _stale_claim(host="dobby", age_s=5)          # a new claim since the scan

    async def stale_scan():
        return rows
    monkeypatch.setattr(sm, "_scan_claims", stale_scan)
    assert await sm.reconcile_stale_moves(fx.f) == []
    assert "restore" not in fx.dobby.calls
    assert (await _row()).moving_to == "rocky"


def _restarted_edge(fx) -> FleetOrchestrator:
    """A new edge process over the same daemons: no live sagas, a fresh start time."""
    router = FleetRouter()
    router.add("dobby", fx.dobby)
    router.add("rocky", fx.rocky)
    return FleetOrchestrator(router, primary="dobby")


@pytest.mark.asyncio
async def test_edge_restart_mid_saga_settles_on_the_first_pass_source_side(fleet):
    """Item 3: the edge died 30 s into a move (after export). A claim older than this
    edge's own start belongs to no live saga — held and settled at once, not in 10 min."""
    fx = fleet
    await fx.dobby.transfer_export(SID, target="rocky")
    await _stale_claim(host="dobby", age_s=30)
    edge = _restarted_edge(fx)
    assert await sm.reconcile_stale_moves(edge) == [SID]
    row = await _row()
    assert (row.host, row.moving_to) == ("dobby", None)
    assert fx.dobby.records[SID].host == "dobby" and SID not in fx.rocky.records
    assert fx.origin == set()
    assert await _lease_holder() == "dobby"


@pytest.mark.asyncio
async def test_edge_restart_target_resumed_outside_the_saga_is_verified_and_kept(
        fleet, monkeypatch):
    """Item 3: after the restamp the edge died; the target got resumed some other way
    and holds the lease with no marker. Past the stall bound it is verified on the
    holder (tree, transcript, live driver), marked, kept — and only then is the source
    finalized. No wedge."""
    fx = fleet
    monkeypatch.setattr(sm, "IMPORT_STALL_S", 0.0, raising=False)
    manifest = await fx.dobby.transfer_export(SID, target="rocky")
    await getattr(sm, "_store_manifest", AsyncMock())(SID, manifest)
    await _stale_claim(host="rocky", moved_from="dobby", age_s=30)
    assert await session_lease.acquire(SID, "rocky", ttl_s=120) is not None
    fx.rocky.trees.add(SID)
    fx.rocky.drivers.add(SID)
    edge = _restarted_edge(fx)
    assert await sm.reconcile_stale_moves(edge) == [SID]
    row = await _row()
    assert (row.host, row.moving_to) == ("rocky", None)
    assert SID in fx.rocky.trees and SID not in fx.dobby.trees
    assert fx.dobby.events[-2:] == [("rocky", "restore"), ("dobby", "finalize")]
    assert await _lease_holder() == "rocky"


@pytest.mark.asyncio
async def test_edge_restart_target_that_fails_verification_falls_back_to_the_source(
        fleet, monkeypatch):
    fx = fleet
    monkeypatch.setattr(sm, "IMPORT_STALL_S", 0.0, raising=False)
    manifest = await fx.dobby.transfer_export(SID, target="rocky")
    await getattr(sm, "_store_manifest", AsyncMock())(SID, manifest)
    await _stale_claim(host="rocky", moved_from="dobby", age_s=30)
    assert await session_lease.acquire(SID, "rocky", ttl_s=120) is not None
    fx.rocky.trees.add(SID)
    fx.rocky.drivers.add(SID)
    fx.rocky.fail["restore"] = sm.MoveError(sm.MOVE_TRANSCRIPT_STALE)
    edge = _restarted_edge(fx)
    assert await sm.reconcile_stale_moves(edge) == [SID]
    row = await _row()
    assert (row.host, row.moving_to) == ("dobby", None)
    _on_source_only_on(fx, edge)
    assert await _lease_holder() == "dobby"


@pytest.mark.asyncio
async def test_lease_without_marker_within_the_stall_bound_still_waits(fleet):
    fx = fleet
    await fx.dobby.transfer_export(SID, target="rocky")
    await _stale_claim(host="rocky", moved_from="dobby", age_s=30)
    assert await session_lease.acquire(SID, "rocky", ttl_s=120) is not None
    edge = _restarted_edge(fx)
    assert await sm.reconcile_stale_moves(edge) == []
    assert SID in fx.dobby.trees and "finalize" not in fx.dobby.calls


def _on_source_only_on(fx, edge) -> None:
    assert fx.dobby.records[SID].host == "dobby"
    assert SID not in fx.rocky.records
    assert SID in fx.dobby.trees and SID not in fx.rocky.trees
    assert edge._host_cache.get(SID) == "dobby"


@pytest.mark.asyncio
async def test_a_source_resumed_after_an_edge_restart_never_runs_on_the_wip(
        monkeypatch, two_hosts, visibility):
    """Item 3, defence in depth on this path: the source daemon restarted too and
    rehydrated the row (still stamped dobby, claim set); its ordinary resume unpacks the
    transfer WIP before anything runs."""
    h = two_hosts
    o = await _source(monkeypatch, h)
    base = _git("rev-parse", "HEAD", cwd=h.cwd)
    (h.cwd / "wip.py").write_text("x\n")
    await o.transfer_export(SID, target="rocky", carry_dirty=True)
    assert _git("rev-parse", "HEAD", cwd=h.cwd) != base        # WIP committed
    async with db_session() as db:
        await db.execute(update(DbSession).where(DbSession.id == SID)
                         .values(moving_to="rocky"))
        await db.commit()
    o._records.clear()
    o._install_row(await _row())                                # what rehydrate builds
    o._ensure_lock = asyncio.Lock()
    o._claim_session = AsyncMock(return_value=True)
    o._ensure_session_cwd = AsyncMock(return_value=True)

    class _Stop(Exception):
        pass

    async def stop(**kw):
        raise _Stop
    import server.sessions.orchestrator as mod
    monkeypatch.setattr(mod, "disabled_for_session", stop)
    with pytest.raises(_Stop):
        await o.ensure_driver(SID)
    assert _git("rev-parse", "HEAD", cwd=h.cwd) == base
    assert (h.cwd / "wip.py").read_text() == "x\n"


# ═════════════════════════════════════════════════════════════════════════════════════
# Final review fix wave
# ═════════════════════════════════════════════════════════════════════════════════════

def _move_warnings(caplog) -> list[str]:
    """WARNING+ lines from the move code on either side (edge saga, daemon halves)."""
    import logging
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
            and r.name in ("server.sessions.session_move", "server.sessions.orchestrator")]


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [
    RuntimeError("fetch failed"), sm.MoveError(sm.MOVE_TRANSCRIPT_STALE),
])
async def test_a_restored_move_logs_no_warning(fleet, caplog, exc):
    """Item 2: `restored` is an expected recovery — INFO only, no auto-filed issue."""
    import logging
    fx = fleet
    fx.rocky.fail["import"] = exc
    with caplog.at_level(logging.INFO):
        with pytest.raises(sm.MoveError) as ei:
            await _move(fx)
    assert ei.value.outcome == "restored"
    assert _move_warnings(caplog) == []


@pytest.mark.asyncio
async def test_a_failed_move_logs_exactly_one_warning_including_its_reconcile(
        fleet, caplog, monkeypatch):
    """Item 2: import failed + restore failed + the claim bookkeeping failed + a stuck
    reconcile pass — one incident, so exactly ONE WARNING: the saga's outcome line."""
    import logging
    fx = fleet
    fx.rocky.fail["import"] = RuntimeError("boom")
    fx.dobby.fail["restore"] = RuntimeError("source down too")

    async def _abandon_fails(sid):
        raise RuntimeError("db blip")
    monkeypatch.setattr(sm, "_abandon_claim", _abandon_fails)
    with caplog.at_level(logging.INFO):
        with pytest.raises(sm.MoveError) as ei:
            await _move(fx)
        assert ei.value.outcome == "failed"
        monkeypatch.undo()
        await _stale_claim(host="dobby")               # still stuck on the next pass
        assert await sm.reconcile_stale_moves(fx.f) == []
    warnings = _move_warnings(caplog)
    assert len(warnings) == 1, warnings
    assert warnings[0].startswith("session.move failed id=")


@pytest.mark.asyncio
async def test_a_failed_restore_that_ended_as_moved_audits_the_real_finalize(fleet):
    """Item 4: the "restore failed but the target holds the lease" path finalizes the
    source; the audit used to say finalized: false, recoveryRef: null regardless."""
    fx = fleet
    real_import = fx.rocky.transfer_import

    async def late(sid, manifest=None, *, source=None, local=False):
        await real_import(sid, manifest, source=source, local=local)
        raise RuntimeError("edge gave up waiting")
    fx.rocky.transfer_import = late
    with pytest.raises(sm.MoveError) as ei:
        await _move(fx)
    assert ei.value.outcome == "moved"
    audit = (await _audits())[-1]
    assert audit["outcome"] == "moved"
    assert audit["finalized"] is True
    assert audit["recoveryRef"] == "refs/viberpit/recovery/5e55a0aa/x-forced"


@pytest.mark.asyncio
async def test_edge_started_at_is_read_from_the_db_clock(fleet, monkeypatch):
    """Item 6: claims are stamped with db_utc_now(); the edge's start must be the same
    clock, read once at startup — before the reconcile's first pass."""
    from datetime import datetime
    fx = fleet
    fx.f.started_at = datetime(2000, 1, 1)           # a wildly skewed edge clock
    db_time = await sm.db_now()
    await fx.f.stamp_started_at()
    assert abs((fx.f.started_at - db_time).total_seconds()) < 5

    seen: list = []

    async def first_pass(sessions, **_k):
        seen.append(sessions.started_at)
        raise asyncio.CancelledError
    fx.f.started_at = datetime(2000, 1, 1)
    monkeypatch.setattr(sm, "reconcile_stale_moves", first_pass)
    with pytest.raises(asyncio.CancelledError):
        await sm.reconcile_loop(fx.f)
    assert seen and seen[0].year != 2000


@pytest.mark.asyncio
async def test_a_failed_db_clock_read_keeps_the_placeholder(fleet, monkeypatch):
    fx = fleet
    before = fx.f.started_at

    async def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(sm, "db_now", boom)
    await fx.f.stamp_started_at()
    assert fx.f.started_at == before
