"""#2910 item 4: rehydrate clears a stale worktree ``index.lock`` for rows whose turn the
restart cut off, and never lets the cleaner break rehydrate."""
from __future__ import annotations

import asyncio

from server.db import db_session
from server.db.models import Session as DbSession
from server.sessions import orchestrator as orch_mod
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory


def _rehydrate(rows: list[tuple[str, str, str]]) -> SessionOrchestrator:
    async def _run():
        async with db_session() as db:
            for sid, status, cwd in rows:
                db.add(DbSession(id=sid, name=sid, account_name="acct",
                                 engine_session_id="c", status=status, cwd=cwd))
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        await orch.rehydrate()
        return orch
    return asyncio.run(_run())


def test_rehydrate_clears_index_lock_only_for_interrupted_rows(isolated_db, tmp_path, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(orch_mod.worktrees, "clear_stale_index_lock",
                        lambda cwd: calls.append(cwd))
    monkeypatch.setattr(orch_mod.worktrees, "is_git_repo", lambda _cwd: False)
    working, idle = tmp_path / "working", tmp_path / "idle"
    working.mkdir()
    idle.mkdir()

    orch = _rehydrate([("w", "working", str(working)), ("i", "idle", str(idle))])

    assert calls == [str(working)]
    assert {"w", "i"} <= set(orch._records)


def test_rehydrate_survives_a_failing_cleaner(isolated_db, tmp_path, monkeypatch):
    def _boom(_cwd):
        raise RuntimeError("boom")
    monkeypatch.setattr(orch_mod.worktrees, "clear_stale_index_lock", _boom)
    monkeypatch.setattr(orch_mod.worktrees, "is_git_repo", lambda _cwd: False)

    orch = _rehydrate([("w", "working", str(tmp_path))])

    assert orch._records["w"].interrupted_by_restart is True
