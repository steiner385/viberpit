"""Archive Audit unarchives a worker's session on THAT worker (#3097).

``list_recent_archived_rows`` spans every host, but the audit loop used the primary
daemon's local ``unarchive``. Its new-style/legacy test (``cwd.exists()``) ran on the
primary's filesystem, so a rocky session whose worktree is intact on rocky read as a
legacy moved-away archive and failed as ArchiveMissingError — handing a restorable
session to a human with advice to fix ``archive_root``.

Covers:
  - unarchive refuses a row stamped with another host (ArchiveOwnedElsewhereError),
    never taking the legacy path.
  - the routing helper sends a foreign row to the owning host's daemon.
  - a foreign row with no enabled Host row raises ArchiveOwnedElsewhereError.
  - a local row still unarchives locally.
  - the audit loop and the human "reopen" decision both use the routing helper.
"""
from __future__ import annotations

import asyncio
import os
from unittest.mock import patch

import pytest
from sqlalchemy import select as sa_select

_ME = "primary-test-host"
_WORKER = "worker-test-host"


def _orch():
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory
    return SessionOrchestrator(sdk_factory=fake_sdk_factory())


async def _setup(tmp_path, *, host, cwd, worker_host_row=True):
    from server.db import db_session, init_db
    from server.db.models import Host, Session as DbSession
    await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'oh.db'}")
    async with db_session() as db:
        db.add(DbSession(id="s1", name="S1", account_name="a", engine_session_id=None,
                         status="archived", cwd=str(cwd), archive_root=None, host=host,
                         archived_by="backlog-runner"))
        if worker_host_row:
            db.add(Host(name=_WORKER, endpoint="https://worker.example:8788", enabled=True))
        await db.commit()


class _FakeRemote:
    def __init__(self, endpoint):
        self.endpoint = endpoint
        self.unarchived: list[str] = []
        self.closed = False

    async def unarchive(self, session_id):
        self.unarchived.append(session_id)
        return None

    async def aclose(self):
        self.closed = True


def _run(tmp_path, coro_fn):
    with patch.dict(os.environ, {"VIBERPIT_HOME": str(tmp_path),
                                 "VIBERPIT_ARCHIVE_HOME": str(tmp_path / "archives")}), \
            patch("server.sessions.orchestrator.self_host_name", return_value=_ME):
        return asyncio.run(coro_fn())


def test_unarchive_refuses_a_row_owned_by_another_host(tmp_path):
    """The exact prod shape: host=worker, archive_root NULL, cwd absent locally. Must
    not be misread as a legacy archive (ArchiveMissingError)."""
    from server.sessions.orchestrator import ArchiveMissingError, ArchiveOwnedElsewhereError

    async def run():
        await _setup(tmp_path, host=_WORKER, cwd=tmp_path / "absent-here")
        with pytest.raises(ArchiveOwnedElsewhereError) as ei:
            await _orch().unarchive("s1")
        assert not isinstance(ei.value, ArchiveMissingError)
        assert ei.value.host == _WORKER

    _run(tmp_path, run)


def test_routing_sends_a_foreign_row_to_the_owning_host(tmp_path):
    from server.db import db_session
    from server.db.models import Session as DbSession
    made: list[_FakeRemote] = []

    def fake_orch_for_endpoint(endpoint):
        made.append(_FakeRemote(endpoint))
        return made[-1]

    async def run():
        await _setup(tmp_path, host=_WORKER, cwd=tmp_path / "absent-here")
        with patch("server.sessions.fleet_orchestrator._orch_for_endpoint",
                   fake_orch_for_endpoint):
            await _orch()._unarchive_on_owning_host("s1")
        async with db_session() as db:
            return (await db.execute(sa_select(DbSession.status).where(
                DbSession.id == "s1"))).scalar_one()

    status = _run(tmp_path, run)
    assert [r.endpoint for r in made] == ["https://worker.example:8788"]
    assert made[0].unarchived == ["s1"] and made[0].closed
    assert status == "archived", "the primary must not touch the worker's row itself"


def test_routing_without_an_enabled_host_row_raises_owned_elsewhere(tmp_path):
    from server.sessions.orchestrator import ArchiveOwnedElsewhereError

    async def run():
        await _setup(tmp_path, host=_WORKER, cwd=tmp_path / "absent-here",
                     worker_host_row=False)
        with pytest.raises(ArchiveOwnedElsewhereError):
            await _orch()._unarchive_on_owning_host("s1")

    _run(tmp_path, run)


@pytest.mark.parametrize("host", [None, _ME])
def test_routing_unarchives_a_local_row_locally(tmp_path, host):
    from server.db import db_session
    from server.db.models import Session as DbSession
    cwd = tmp_path / "wt"
    cwd.mkdir()

    async def run():
        await _setup(tmp_path, host=host, cwd=cwd)
        with patch("server.sessions.fleet_orchestrator._orch_for_endpoint",
                   side_effect=AssertionError("must not route a local row")):
            rec = await _orch()._unarchive_on_owning_host("s1")
        async with db_session() as db:
            status = (await db.execute(sa_select(DbSession.status).where(
                DbSession.id == "s1"))).scalar_one()
        return rec, status

    rec, status = _run(tmp_path, run)
    assert rec is not None and status == "idle"


def test_audit_loop_and_reopen_decision_use_the_routing_helper(tmp_path):
    """Both primary-side callers of the archive-audit unarchive go through routing."""
    import inspect
    from server.sessions.orchestrator import SessionOrchestrator

    async def run():
        await _setup(tmp_path, host=None, cwd=tmp_path)
        orch = _orch()
        assert orch._archaudit_loop._unarchive_fn == orch._unarchive_on_owning_host

    _run(tmp_path, run)
    src = inspect.getsource(SessionOrchestrator.archive_audit_decide_finding)
    assert "_unarchive_on_owning_host" in src and "self.unarchive(" not in src


_CLIENT_TLS_VARS = tuple(f"{p}_SESSIOND_CLIENT_{k}" for p in ("VIBERPIT", "CCSWITCH")
                         for k in ("CERT", "KEY", "CA"))


def test_routing_without_client_tls_fails_fast_with_a_precondition_error(tmp_path,
                                                                         monkeypatch):
    """#3142: the primary daemon holds no mTLS client identity (only the edge does),
    so dialing a worker's https endpoint must raise a typed owned-elsewhere error that
    says why, never a raw TLSConfigError the audit loop logs as a WARNING every tick."""
    from server.ipc.tls import TLSConfigError
    from server.sessions.orchestrator import (ArchiveOwnedElsewhereError,
                                              ArchiveOwnerUnreachableError)
    for var in _CLIENT_TLS_VARS:
        monkeypatch.delenv(var, raising=False)

    async def run():
        await _setup(tmp_path, host=_WORKER, cwd=tmp_path / "absent-here")
        with pytest.raises(ArchiveOwnerUnreachableError) as ei:
            await _orch()._unarchive_on_owning_host("s1")
        return ei.value

    err = _run(tmp_path, run)
    assert isinstance(err, ArchiveOwnedElsewhereError)
    assert err.host == _WORKER and err.session_id == "s1"
    assert isinstance(err.__cause__, TLSConfigError)
    assert "VIBERPIT_SESSIOND_CLIENT_CERT" in str(err) and _WORKER in str(err)
