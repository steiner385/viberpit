"""Worktree controls (D-19): preflight, Discard and Repair for a session.

Plan: docs/superpowers/plans/2026-11-06-desktop-parity-d19-leftovers.md, Task 1.
Review Focus 1-3: Discard never loses work silently (recovery refs for every dirty
tree and unpushed commit, only a session branch deleted); Discard and Repair are
refused for a busy session, during a move, for a non-managed cwd and from a token
caller; Repair never modifies a present tree. Real git under ``tmp_path``, with a
local bare ``origin`` so "unpushed" means what it does in production.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import select

from viberpit import gitrepos, worktrees
from server.db import db_session, init_db
from server.db.models import AuditEvent, Session as DbSession
from server.ipc.client import DaemonError
from server.sessions import session_actions
from server.sessions import worktree_controls as wc
from server.sessions.facade import InProcessOrchestratorAdapter
from server.sessions.fleet_orchestrator import _MOVE_BLOCKED, _PER_SESSION, FleetOrchestrator
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.remote import RemoteOrchestrator
from server.sessions.session_actions import ActionError
from server.sessions.session_move import SessionMoving
from server.tests.fakes.fake_sdk import FakeSDKClient
from server.tests.test_sessions_actions_api import _ENV, _H, _audit_events, _make_client

REPO = "owner/repo"
SID = "abcd1234-5678-0000-0000-000000000000"
OTHER = "eeee9999-5678-0000-0000-000000000000"


def _git(*args: str, cwd: str | Path) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _factory(cwd, cfg, can_use_tool, mode, resume, *, on_pre_compact=None,
             options_overlay=None):
    return FakeSDKClient([], on_can_use_tool=can_use_tool)


@pytest.fixture
def env(tmp_path):
    """A managed clone of REPO with a bare ``origin``, ``feature`` pushed, and a session
    worktree on its own CHILD branch (``feature`` is checked out in the clone, so
    create_worktree makes ``viberpit/feature-<short>``)."""
    root = tmp_path / "root"
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", str(origin), "-b", "main"], check=True,
                   capture_output=True)
    clone = gitrepos.clone_path(str(root), REPO)
    clone.parent.mkdir(parents=True)
    subprocess.run(["git", "init", str(clone), "-b", "main"], check=True, capture_output=True)
    _git("config", "user.email", "t@example.com", cwd=clone)
    _git("config", "user.name", "Test", cwd=clone)
    (clone / "README.md").write_text("hi\n")
    _git("add", "-A", cwd=clone)
    _git("commit", "-m", "init", cwd=clone)
    _git("remote", "add", "origin", str(origin), cwd=clone)
    _git("checkout", "-b", "feature", cwd=clone)
    _git("push", "-q", "origin", "main", "feature", cwd=clone)
    info = worktrees.create_worktree(str(clone), "feature", SID, str(root))
    assert info.is_child, "the session must run on its own session branch"
    return {"root": root, "clone": clone, "cwd": info.path, "branch": info.branch,
            "db_url": f"sqlite+aiosqlite:///{tmp_path / 'test.db'}", "tmp": tmp_path}


async def _orch(env, *, cwd: str | None = None, status: str = "idle",
                branch: str | None = None, extra_rows: tuple = ()):
    await init_db(db_url=env["db_url"])
    async with db_session() as db:
        db.add(DbSession(id=SID, name="t", account_name="acct1", engine_session_id="csid",
                         status=status, cwd=cwd or env["cwd"], repo=REPO,
                         branch=branch or env["branch"]))
        for row in extra_rows:
            db.add(row)
    orch = SessionOrchestrator(sdk_factory=_factory, repo_root=str(env["root"]))
    await orch.rehydrate()
    # rehydrate heals a mid-turn status left by a dead process; pin the live one.
    orch._records[SID].status = status
    return orch


def _dirty_and_commit(env) -> str:
    """One unpushed commit on the session branch plus a dirty tree."""
    cwd = Path(env["cwd"])
    (cwd / "work.txt").write_text("committed, never pushed\n")
    _git("add", "-A", cwd=cwd)
    _git("-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "-m", "work", cwd=cwd)
    tip = _git("rev-parse", "HEAD", cwd=cwd)
    (cwd / "README.md").write_text("unstaged edit\n")
    (cwd / "untracked.txt").write_text("precious\n")
    return tip


def _row_status() -> str:
    async def read():
        async with db_session() as db:
            return (await db.get(DbSession, SID)).status
    return asyncio.run(read())


def _branch_exists(clone, branch: str) -> bool:
    return subprocess.run(["git", "-C", str(clone), "rev-parse", "--verify", "--quiet",
                           f"refs/heads/{branch}"], capture_output=True).returncode == 0


# ── preflight ────────────────────────────────────────────────────────────────


def test_preflight_reports_dirty_and_unpushed_counts(env):
    _dirty_and_commit(env)
    facts = asyncio.run(_run_preflight(env))
    assert facts["managed"] is True and facts["present"] is True
    assert facts["state"] == "present"
    assert facts["dirty"] == 2 and facts["unpushed"] == 1
    assert facts["branch"] == env["branch"] and facts["sessionBranch"] is True
    assert facts["busy"] is False and facts["canDiscard"] is True
    assert facts["canRepair"] is False
    assert facts["restoreHint"] == worktrees.restore_command("<recovery-ref>")


async def _run_preflight(env, **kw):
    orch = await _orch(env, **kw)
    return await orch.worktree_preflight(SID)


def test_preflight_of_a_clean_pushed_tree_has_no_restore_hint(env):
    facts = asyncio.run(_run_preflight(env))
    assert facts["dirty"] == 0 and facts["unpushed"] == 0
    assert facts["restoreHint"] is None and facts["canDiscard"] is True


def test_preflight_of_a_missing_worktree_offers_repair(env):
    shutil.rmtree(env["cwd"])
    facts = asyncio.run(_run_preflight(env))
    assert facts["state"] == "missing" and facts["present"] is False
    assert facts["canRepair"] is True and facts["canDiscard"] is False


def test_preflight_of_an_unmanaged_cwd(env):
    plain = env["tmp"] / "plain"
    plain.mkdir()
    facts = asyncio.run(_run_preflight(env, cwd=str(plain)))
    assert facts["managed"] is False
    assert facts["canDiscard"] is False and facts["canRepair"] is False


def test_preflight_unknown_session_is_a_key_error(env):
    async def run():
        orch = await _orch(env)
        await orch.worktree_preflight("nope")
    with pytest.raises(KeyError):
        asyncio.run(run())


# ── discard ──────────────────────────────────────────────────────────────────


def test_discard_keeps_every_byte_then_archives_and_removes(env, caplog):
    tip = _dirty_and_commit(env)

    async def run():
        orch = await _orch(env)
        return await orch.discard_worktree(SID)

    with caplog.at_level("INFO"):
        res = asyncio.run(run())
    assert res["discarded"] is True and res["branchDeleted"] is True
    refs = res["recoveryRefs"]
    assert [r.rsplit("-", 1)[-1] for r in refs] == ["discard", "branch"]
    assert res["restoreCommand"] == worktrees.restore_command(refs[0])
    # The tree and the session branch are gone; the user's branch is not.
    assert not Path(env["cwd"]).exists()
    assert not _branch_exists(env["clone"], env["branch"])
    assert _branch_exists(env["clone"], "feature")
    # Every byte is reachable from the refs.
    clone = env["clone"]
    files = _git("ls-tree", "-r", "--name-only", refs[0], cwd=clone).splitlines()
    assert {"untracked.txt", "work.txt", "README.md"} <= set(files)
    assert _git("show", f"{refs[0]}:README.md", cwd=clone) == "unstaged edit"
    assert _git("rev-parse", refs[1], cwd=clone) == tip
    # remove_worktree's own forced/branch snapshots find these refs: no duplicates.
    assert sorted(worktrees.recovery_refs(str(clone), SID)) == sorted(refs)
    assert _row_status() == "archived"
    assert any("session.worktree_discarded" in r.getMessage() for r in caplog.records)


def test_discard_of_a_clean_tree_writes_no_ref(env):
    async def run():
        orch = await _orch(env)
        return await orch.discard_worktree(SID)

    res = asyncio.run(run())
    assert res["recoveryRefs"] == [] and res["restoreCommand"] is None
    assert not Path(env["cwd"]).exists()


@pytest.mark.parametrize("status", ["in_progress", "condensing", "retrying", "working",
                                    "waiting_external"])
def test_discard_refuses_a_busy_session_untouched(env, status, caplog):
    _dirty_and_commit(env)

    async def run():
        orch = await _orch(env, status=status)
        await orch.discard_worktree(SID)

    with caplog.at_level("INFO"), pytest.raises(wc.WorktreeControlError) as ei:
        asyncio.run(run())
    assert ei.value.code == wc.WORKTREE_SESSION_BUSY
    assert (Path(env["cwd"]) / "untracked.txt").read_text() == "precious\n"
    assert worktrees.recovery_refs(str(env["clone"]), SID) == []
    assert _row_status() != "archived"
    refused = [r for r in caplog.records if "worktree_discard_refused" in r.getMessage()]
    assert refused and all(r.levelname == "INFO" for r in refused)


def test_discard_refuses_a_live_turn(env):
    class _Busy:
        turn_active = True
        drain_alive = False
        awaiting_user_input = False

    async def run():
        orch = await _orch(env)
        orch._drivers[SID] = _Busy()
        try:
            await orch.discard_worktree(SID)
        finally:
            orch._drivers.pop(SID, None)

    with pytest.raises(wc.WorktreeControlError) as ei:
        asyncio.run(run())
    assert ei.value.code == wc.WORKTREE_SESSION_BUSY
    assert Path(env["cwd"]).exists()


def test_discard_refuses_an_unmanaged_cwd_untouched(env):
    plain = env["tmp"] / "plain"
    plain.mkdir()
    (plain / "mine.txt").write_text("user's\n")

    async def run():
        orch = await _orch(env, cwd=str(plain))
        await orch.discard_worktree(SID)

    with pytest.raises(wc.WorktreeControlError) as ei:
        asyncio.run(run())
    assert ei.value.code == wc.WORKTREE_NOT_MANAGED
    assert (plain / "mine.txt").read_text() == "user's\n"


def test_discard_refuses_the_shared_clone(env):
    async def run():
        orch = await _orch(env, cwd=str(env["clone"]))
        await orch.discard_worktree(SID)

    with pytest.raises(wc.WorktreeControlError) as ei:
        asyncio.run(run())
    assert ei.value.code == wc.WORKTREE_NOT_MANAGED
    assert (env["clone"] / "README.md").exists()


def test_discard_refuses_a_missing_worktree(env):
    shutil.rmtree(env["cwd"])

    async def run():
        orch = await _orch(env)
        await orch.discard_worktree(SID)

    with pytest.raises(wc.WorktreeControlError) as ei:
        asyncio.run(run())
    assert ei.value.code == wc.WORKTREE_MISSING


def test_discard_refuses_a_worktree_another_session_runs_in(env):
    other = DbSession(id=OTHER, name="o", account_name="acct1", engine_session_id="c2",
                      status="idle", cwd=env["cwd"], repo=REPO, branch=env["branch"])

    async def run():
        orch = await _orch(env, extra_rows=(other,))
        await orch.discard_worktree(SID)

    with pytest.raises(wc.WorktreeControlError) as ei:
        asyncio.run(run())
    assert ei.value.code == wc.WORKTREE_SHARED
    assert Path(env["cwd"]).exists()


def test_discard_refuses_when_the_snapshot_fails_and_touches_nothing(env, monkeypatch):
    _dirty_and_commit(env)
    monkeypatch.setattr(worktrees, "snapshot_before_discard", lambda *a, **k: None)

    async def run():
        orch = await _orch(env)
        await orch.discard_worktree(SID)

    with pytest.raises(wc.WorktreeControlError) as ei:
        asyncio.run(run())
    assert ei.value.code == wc.WORKTREE_SNAPSHOT_FAILED
    assert (Path(env["cwd"]) / "untracked.txt").exists()
    assert _branch_exists(env["clone"], env["branch"])
    assert _row_status() == "idle"


def test_a_failed_removal_keeps_the_snapshot_and_names_it(env, monkeypatch):
    _dirty_and_commit(env)
    monkeypatch.setattr(worktrees, "remove_worktree", lambda *a, **k: False)

    async def run():
        orch = await _orch(env)
        await orch.discard_worktree(SID)

    with pytest.raises(wc.WorktreeControlError) as ei:
        asyncio.run(run())
    assert ei.value.code == wc.WORKTREE_REMOVE_FAILED
    refs = ei.value.extra["recoveryRefs"]
    assert len(refs) == 2 and set(refs) <= set(
        worktrees.recovery_refs(str(env["clone"]), SID))
    assert ei.value.extra["restoreCommand"] == worktrees.restore_command(refs[0])
    assert ei.value.error_data["code"] == wc.WORKTREE_REMOVE_FAILED
    assert _row_status() == "archived"


def test_a_failed_archive_keeps_the_snapshot_and_the_tree(env, monkeypatch):
    _dirty_and_commit(env)

    async def run():
        orch = await _orch(env)

        async def boom(*a, **k):
            raise RuntimeError("db down")
        monkeypatch.setattr(orch, "archive_session", boom)
        await orch.discard_worktree(SID)

    with pytest.raises(wc.WorktreeControlError) as ei:
        asyncio.run(run())
    assert ei.value.code == wc.WORKTREE_DISCARD_FAILED
    assert len(ei.value.extra["recoveryRefs"]) == 2
    assert (Path(env["cwd"]) / "untracked.txt").exists()


# ── repair ───────────────────────────────────────────────────────────────────


def test_repair_recreates_a_missing_worktree_from_its_branch(env):
    shutil.rmtree(env["cwd"])

    async def run():
        orch = await _orch(env)
        res = await orch.repair_worktree(SID)
        async with db_session() as db:
            audits = (await db.execute(select(AuditEvent).where(
                AuditEvent.type == "session.worktree_recreated"))).scalars().all()
        return res, audits

    res, audits = asyncio.run(run())
    assert res["repaired"] is True and res["path"] == env["cwd"]
    assert worktrees.current_branch(env["cwd"]) == env["branch"]
    assert len(audits) == 1


def test_repair_recreates_an_empty_directory_in_place(env):
    shutil.rmtree(env["cwd"])
    Path(env["cwd"]).mkdir()

    async def run():
        orch = await _orch(env)
        return await orch.repair_worktree(SID)

    assert asyncio.run(run())["repaired"] is True
    assert worktrees.current_branch(env["cwd"]) == env["branch"]


def test_repair_never_touches_a_present_tree(env):
    _dirty_and_commit(env)
    cwd = Path(env["cwd"])
    inode = cwd.stat().st_ino
    before = sorted(os.listdir(cwd))

    async def run():
        orch = await _orch(env)
        await orch.repair_worktree(SID)

    with pytest.raises(wc.WorktreeControlError) as ei:
        asyncio.run(run())
    assert ei.value.code == wc.WORKTREE_PRESENT
    assert cwd.stat().st_ino == inode and sorted(os.listdir(cwd)) == before
    assert (cwd / "untracked.txt").read_text() == "precious\n"


def test_repair_never_touches_a_non_empty_gitless_dir(env):
    shutil.rmtree(env["cwd"])
    Path(env["cwd"]).mkdir()
    (Path(env["cwd"]) / "stray.txt").write_text("not ours\n")

    async def run():
        orch = await _orch(env)
        await orch.repair_worktree(SID)

    with pytest.raises(wc.WorktreeControlError) as ei:
        asyncio.run(run())
    assert ei.value.code == wc.WORKTREE_NOT_EMPTY
    assert (Path(env["cwd"]) / "stray.txt").read_text() == "not ours\n"


def test_repair_refuses_a_busy_session(env):
    shutil.rmtree(env["cwd"])

    async def run():
        orch = await _orch(env, status="in_progress")
        await orch.repair_worktree(SID)

    with pytest.raises(wc.WorktreeControlError) as ei:
        asyncio.run(run())
    assert ei.value.code == wc.WORKTREE_SESSION_BUSY
    assert not Path(env["cwd"]).exists()


def test_repair_refuses_an_unmanaged_cwd(env):
    async def run():
        orch = await _orch(env, cwd=str(env["tmp"] / "gone"))
        await orch.repair_worktree(SID)

    with pytest.raises(wc.WorktreeControlError) as ei:
        asyncio.run(run())
    assert ei.value.code == wc.WORKTREE_NOT_MANAGED
    assert not (env["tmp"] / "gone").exists()


def test_repair_without_the_branch_fails_and_fabricates_nothing(env):
    shutil.rmtree(env["cwd"])

    async def run():
        orch = await _orch(env, branch="gone-branch")
        await orch.repair_worktree(SID)

    with pytest.raises(wc.WorktreeControlError) as ei:
        asyncio.run(run())
    assert ei.value.code == wc.WORKTREE_REPAIR_FAILED
    assert not Path(env["cwd"]).exists()


# ── session_actions: person-only, confirm, audit ─────────────────────────────


class _Orch:
    def __init__(self, error: Exception | None = None):
        self.calls: list[tuple[str, str]] = []
        self.error = error

    async def worktree_preflight(self, sid):
        self.calls.append(("preflight", sid))
        return {"sessionId": sid}

    async def discard_worktree(self, sid):
        self.calls.append(("discard", sid))
        if self.error is not None:
            raise self.error
        return {"discarded": True, "recoveryRefs": ["refs/x"], "restoreCommand": "r",
                "path": "/p", "branch": "viberpit/b", "branchDeleted": True}

    async def repair_worktree(self, sid):
        self.calls.append(("repair", sid))
        if self.error is not None:
            raise self.error
        return {"repaired": True, "path": "/p", "branch": "b", "recoveryRefs": []}


@pytest.mark.parametrize("call", ["preflight", "discard", "repair"])
@pytest.mark.parametrize("identity,via", [("system", None), ("u@x", "assistant"),
                                          ("u@x", "local-process")])
def test_every_control_is_person_only(call, identity, via):
    from server.sessions.types import SYSTEM_IDENTITY
    from server.auth.identity import LOCAL_PROCESS_VIA
    identity = SYSTEM_IDENTITY if identity == "system" else identity
    via = LOCAL_PROCESS_VIA if via == "local-process" else via
    orch = _Orch()

    async def run():
        if call == "preflight":
            await session_actions.worktree_preflight(orch, session_id=SID, identity=identity,
                                                     via=via)
        elif call == "discard":
            await session_actions.discard_worktree(orch, None, session_id=SID, confirm=True,
                                                   identity=identity, via=via)
        else:
            await session_actions.repair_worktree(orch, None, session_id=SID,
                                                  identity=identity, via=via)

    with pytest.raises(ActionError) as ei:
        asyncio.run(run())
    assert ei.value.http == 403 and ei.value.code == "SESSION_CONTROL_HUMAN_ONLY"
    assert orch.calls == []


def test_discard_needs_confirm(caplog):
    orch = _Orch()

    async def run():
        await session_actions.discard_worktree(orch, None, session_id=SID, confirm=False,
                                               identity="u@x", via=None)

    with caplog.at_level("INFO"), pytest.raises(ActionError) as ei:
        asyncio.run(run())
    assert ei.value.http == 400 and ei.value.code == wc.WORKTREE_CONFIRM_REQUIRED
    assert orch.calls == []


@pytest.mark.parametrize("code,http", [
    (wc.WORKTREE_SESSION_BUSY, 409), (wc.WORKTREE_NOT_MANAGED, 409),
    (wc.WORKTREE_MISSING, 409), (wc.WORKTREE_SHARED, 409),
    (wc.WORKTREE_SNAPSHOT_FAILED, 500), (wc.WORKTREE_DISCARD_FAILED, 500),
    (wc.WORKTREE_REMOVE_FAILED, 500), (wc.WORKTREE_PRESENT, 409),
    (wc.WORKTREE_NOT_EMPTY, 409), (wc.WORKTREE_REPAIR_FAILED, 409),
])
def test_every_code_has_copy_and_a_status(code, http):
    assert wc.http_status(code) == http and wc.MESSAGES[code]
    assert code in wc.WORKTREE_CONTROL_CODES


# ── routes ───────────────────────────────────────────────────────────────────


def _route_client(error: Exception | None = None):
    client, fake = _make_client()
    orch = _Orch(error)
    fake.worktree_preflight = orch.worktree_preflight
    fake.discard_worktree = orch.discard_worktree
    fake.repair_worktree = orch.repair_worktree
    return client, orch


def test_discard_route_runs_and_audits():
    client, orch = _route_client()
    with patch.dict(os.environ, _ENV), client:
        r = client.post(f"/api/sessions/{SID}/worktree/discard", json={"confirm": True},
                        headers=_H)
        events = client.portal.call(_audit_events)
    assert r.status_code == 200, r.text
    assert r.json()["recoveryRefs"] == ["refs/x"]
    assert orch.calls == [("discard", SID)]
    audits = [e[2] for e in events if e[0] == "session.worktree_discard"]
    assert audits == [{"sessionId": SID, "outcome": "discarded", "path": "/p",
                       "branch": "viberpit/b", "branchDeleted": True,
                       "recoveryRefs": ["refs/x"]}]


def test_discard_route_without_confirm_is_400():
    client, orch = _route_client()
    with patch.dict(os.environ, _ENV), client:
        r = client.post(f"/api/sessions/{SID}/worktree/discard", json={}, headers=_H)
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == wc.WORKTREE_CONFIRM_REQUIRED
    assert orch.calls == []


def test_discard_route_failure_after_snapshot_names_the_refs_and_audits():
    err = wc.WorktreeControlError(wc.WORKTREE_REMOVE_FAILED,
                                  {"recoveryRefs": ["refs/a"], "restoreCommand": "r"})
    client, _ = _route_client(err)
    with patch.dict(os.environ, _ENV), client:
        r = client.post(f"/api/sessions/{SID}/worktree/discard", json={"confirm": True},
                        headers=_H)
        events = client.portal.call(_audit_events)
    assert r.status_code == 500
    detail = r.json()["detail"]
    assert detail["code"] == wc.WORKTREE_REMOVE_FAILED
    assert detail["recoveryRefs"] == ["refs/a"] and detail["restoreCommand"] == "r"
    assert [e[2]["outcome"] for e in events if e[0] == "session.worktree_discard"] == [
        wc.WORKTREE_REMOVE_FAILED]


def test_discard_route_refusal_writes_no_audit():
    client, _ = _route_client(wc.WorktreeControlError(wc.WORKTREE_SESSION_BUSY))
    with patch.dict(os.environ, _ENV), client:
        r = client.post(f"/api/sessions/{SID}/worktree/discard", json={"confirm": True},
                        headers=_H)
        events = client.portal.call(_audit_events)
    assert r.status_code == 409 and r.json()["detail"]["code"] == wc.WORKTREE_SESSION_BUSY
    assert not [e for e in events if e[0] == "session.worktree_discard"]


def test_repair_route_runs_and_audits():
    client, orch = _route_client()
    with patch.dict(os.environ, _ENV), client:
        r = client.post(f"/api/sessions/{SID}/worktree/repair", headers=_H)
        events = client.portal.call(_audit_events)
    assert r.status_code == 200, r.text
    assert orch.calls == [("repair", SID)]
    assert [e[2] for e in events if e[0] == "session.worktree_repair"] == [
        {"sessionId": SID, "path": "/p", "branch": "b"}]


def test_preflight_route():
    client, orch = _route_client()
    with patch.dict(os.environ, _ENV), client:
        r = client.get(f"/api/sessions/{SID}/worktree/preflight", headers=_H)
    assert r.status_code == 200 and r.json() == {"sessionId": SID}


@pytest.mark.parametrize("path,method", [("discard", "post"), ("repair", "post"),
                                         ("preflight", "get")])
def test_routes_are_person_only(path, method, monkeypatch):
    from server.auth import identity
    monkeypatch.setattr(identity, "attested_front_end_hop", lambda _conn: False)
    client, orch = _route_client()
    with patch.dict(os.environ, _ENV), client:
        kw = {"json": {"confirm": True}} if path == "discard" else {}
        r = getattr(client, method)(f"/api/sessions/{SID}/worktree/{path}", headers=_H, **kw)
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "SESSION_CONTROL_HUMAN_ONLY"
    assert orch.calls == []


@pytest.mark.parametrize("path,method", [("discard", "post"), ("repair", "post"),
                                         ("preflight", "get")])
def test_routes_map_unknown_session_and_old_daemons(path, method):
    for error, status, code in [(KeyError(SID), 404, None),
                                (DaemonError("UnknownMethod", path), 409, "daemon_outdated")]:
        client, orch = _route_client(error)

        async def raising(sid, _e=error):
            raise _e
        setattr(client.app.state.sessions, "worktree_preflight", raising)
        with patch.dict(os.environ, _ENV), client:
            kw = {"json": {"confirm": True}} if path == "discard" else {}
            r = getattr(client, method)(f"/api/sessions/{SID}/worktree/{path}", headers=_H,
                                        **kw)
        assert r.status_code == status, (path, error)
        if code:
            assert r.json()["detail"]["code"] == code


# ── RPC / remote / facade / fleet ────────────────────────────────────────────


class _RecordingClient:
    def __init__(self, result=None, error: Exception | None = None):
        self.result, self.error = result, error
        self.calls: list[tuple[str, dict]] = []

    async def call(self, method, *, timeout=None, **kwargs):
        self.calls.append((method, kwargs))
        if self.error is not None:
            raise self.error
        return self.result


@pytest.mark.asyncio
async def test_remote_forwards_and_rebuilds_refusals_with_their_refs():
    client = _RecordingClient(result={"ok": 1})
    ro = RemoteOrchestrator(client)
    await ro.worktree_preflight(SID)
    await ro.discard_worktree(SID)
    await ro.repair_worktree(SID)
    assert [c[0] for c in client.calls] == ["worktree_preflight", "discard_worktree",
                                           "repair_worktree"]
    assert all(c[1] == {"session_id": SID} for c in client.calls)
    with pytest.raises(wc.WorktreeControlError) as ei:
        await RemoteOrchestrator(_RecordingClient(error=DaemonError(
            "WorktreeControlError", wc.WORKTREE_REMOVE_FAILED,
            {"code": wc.WORKTREE_REMOVE_FAILED, "recoveryRefs": ["refs/a"],
             "restoreCommand": "r"}))).discard_worktree(SID)
    assert ei.value.code == wc.WORKTREE_REMOVE_FAILED
    assert ei.value.extra == {"recoveryRefs": ["refs/a"], "restoreCommand": "r"}
    with pytest.raises(KeyError):
        await RemoteOrchestrator(_RecordingClient(
            error=DaemonError("KeyError", "nope"))).repair_worktree("nope")
    with pytest.raises(DaemonError):  # an unknown code is never invented
        await RemoteOrchestrator(_RecordingClient(error=DaemonError(
            "WorktreeControlError", "WAT", {"code": "WAT"}))).discard_worktree(SID)


@pytest.mark.asyncio
async def test_a_refusal_crosses_the_real_daemon_envelope(tmp_path):
    from server.ipc.client import DaemonClient
    from server.tests.ipc_harness import serve_daemon_uds
    from server.ipc.daemon import create_daemon_app
    from server.ipc.orchestrator_rpc import register_orchestrator_rpc

    class _O(SessionOrchestrator):
        async def discard_worktree(self, session_id):
            raise wc.WorktreeControlError(wc.WORKTREE_REMOVE_FAILED,
                                          {"recoveryRefs": ["refs/a"]})

        async def worktree_preflight(self, session_id):
            return {"sessionId": session_id, "dirty": 1}

    app = create_daemon_app()
    register_orchestrator_rpc(app, _O(sdk_factory=_factory, repo_root=str(tmp_path)))
    async with serve_daemon_uds(app) as sock:
        client = DaemonClient(sock)
        try:
            ro = RemoteOrchestrator(client)
            facts = await ro.worktree_preflight(SID)
            with pytest.raises(wc.WorktreeControlError) as ei:
                await ro.discard_worktree(SID)
        finally:
            await client.aclose()
    assert facts == {"sessionId": SID, "dirty": 1}
    assert ei.value.code == wc.WORKTREE_REMOVE_FAILED
    assert ei.value.extra == {"recoveryRefs": ["refs/a"]}


@pytest.mark.asyncio
async def test_fleet_routes_to_the_owner_and_blocks_mutations_during_a_move():
    assert {"worktree_preflight", "discard_worktree", "repair_worktree"} <= _PER_SESSION
    assert {"discard_worktree", "repair_worktree"} <= _MOVE_BLOCKED
    assert "worktree_preflight" not in _MOVE_BLOCKED

    class _HostOrch:
        def __init__(self, name):
            self.name, self.calls = name, []

        async def worktree_preflight(self, sid):
            self.calls.append("preflight")
            return {"host": self.name}

        async def discard_worktree(self, sid):
            self.calls.append("discard")
            return {"host": self.name}

        async def repair_worktree(self, sid):
            self.calls.append("repair")
            return {"host": self.name}

    class _Router:
        def __init__(self, orchs):
            self._orchs = orchs

        def get(self, name):
            return self._orchs[name]

        def names(self):
            return sorted(self._orchs)

    dobby, rocky = _HostOrch("dobby"), _HostOrch("rocky")
    fo = FleetOrchestrator(_Router({"dobby": dobby, "rocky": rocky}), primary="dobby")  # type: ignore[arg-type]
    fo._host_cache[SID] = "rocky"
    assert (await fo.worktree_preflight(SID))["host"] == "rocky"
    assert (await fo.discard_worktree(SID))["host"] == "rocky"
    assert (await fo.repair_worktree(SID))["host"] == "rocky"
    assert rocky.calls == ["preflight", "discard", "repair"] and dobby.calls == []
    fo._moving[SID] = "dobby"
    for name in ("discard_worktree", "repair_worktree"):
        with pytest.raises(SessionMoving):
            await getattr(fo, name)(SID)
    assert (await fo.worktree_preflight(SID))["host"] == "rocky"
    assert rocky.calls == ["preflight", "discard", "repair", "preflight"]
    adapter = InProcessOrchestratorAdapter(dobby)  # type: ignore[arg-type]
    assert (await adapter.discard_worktree(SID))["host"] == "dobby"
