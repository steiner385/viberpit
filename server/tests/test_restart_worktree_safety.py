"""Restart worktree data safety (#2913, #2914; part of #2910).

Spec: docs/superpowers/specs/2026-10-11-restart-worktree-data-safety-design.md.

A missing MANAGED worktree is never silently turned into an empty directory: it is
recreated from its branch (loudly) or the driver build is refused. Destructive paths
and every drain leave a recovery ref first. Real git repos under ``tmp_path``.
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from viberpit import gitrepos, worktrees
from server.db import db_session, init_db
from server.db.models import AuditEvent, Session as DbSession
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_sdk import FakeSDKClient

REPO = "owner/repo"


def _git(*args: str, cwd: str | Path) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _factory(cwd, cfg, can_use_tool, mode, resume, *, on_pre_compact=None,
             options_overlay=None):
    return FakeSDKClient([], on_can_use_tool=can_use_tool)


@pytest.fixture
def env(tmp_path):
    """A managed clone of REPO under tmp_path plus a session worktree on `feature`."""
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
    sid = "abcd1234-5678-0000-0000-000000000000"
    info = worktrees.create_worktree(str(clone), "feature", sid, str(root))
    return {"root": root, "clone": clone, "sid": sid, "cwd": info.path,
            "db_url": f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
            "cfg": tmp_path / "cfg"}


async def _orch(env, *, cwd: str, branch: str | None = "feature", status: str = "idle"):
    await init_db(db_url=env["db_url"])
    async with db_session() as db:
        db.add(DbSession(id=env["sid"], name="t", account_name="acct1",
                         engine_session_id="csid", status=status, cwd=cwd,
                         repo=REPO, branch=branch))
    orch = SessionOrchestrator(sdk_factory=_factory, repo_root=str(env["root"]))
    await orch.rehydrate()
    return orch


async def _ensure(orch, env):
    env["cfg"].mkdir(parents=True, exist_ok=True)
    with patch("server.sessions.orchestrator.compose", return_value=env["cfg"]), \
         patch("server.sessions.orchestrator.configdir"):
        return await orch.ensure_driver(env["sid"])


async def _close(orch, sid):
    drv = orch._drivers.pop(sid, None)
    if drv is not None:
        await drv.aclose()


def test_missing_managed_worktree_is_recreated_from_its_branch(env, caplog):
    shutil.rmtree(env["cwd"])

    async def run():
        orch = await _orch(env, cwd=env["cwd"])
        drv = await _ensure(orch, env)
        async with db_session() as db:
            audits = (await db.execute(select(AuditEvent).where(
                AuditEvent.type == "session.worktree_recreated"))).scalars().all()
        await _close(orch, env["sid"])
        return orch, drv, audits

    with caplog.at_level("WARNING"):
        orch, drv, audits = asyncio.run(run())
    assert drv is not None
    assert worktrees.current_branch(env["cwd"]) == "feature", \
        "the recreated path must be a real git worktree on the session's branch"
    assert any("worktree.recreated" in r.getMessage() for r in caplog.records)
    assert len(audits) == 1 and audits[0].detail["branch"] == "feature"
    preamble = orch._pending_prompt_preambles[env["sid"]]
    assert len(preamble) == 1 and "feature" in preamble[0]


def test_missing_managed_worktree_without_branch_refuses_the_build(env, caplog):
    shutil.rmtree(env["cwd"])

    async def run():
        orch = await _orch(env, cwd=env["cwd"], branch="gone-branch")
        return await _ensure(orch, env)

    with caplog.at_level("WARNING"):
        drv = asyncio.run(run())
    assert drv is None
    assert not Path(env["cwd"]).exists(), "no empty directory may be fabricated"
    assert any("worktree.missing" in r.getMessage() for r in caplog.records)


def test_existing_worktree_is_never_recreated(env):
    marker = Path(env["cwd"]) / "uncommitted.txt"
    marker.write_text("precious\n")
    inode = Path(env["cwd"]).stat().st_ino

    async def run():
        orch = await _orch(env, cwd=env["cwd"])
        drv = await _ensure(orch, env)
        await _close(orch, env["sid"])
        return orch, drv

    orch, drv = asyncio.run(run())
    assert drv is not None
    assert marker.read_text() == "precious\n"
    assert Path(env["cwd"]).stat().st_ino == inode
    assert env["sid"] not in orch._pending_prompt_preambles


def test_missing_scratch_dir_is_still_created(env):
    scratch = worktrees.scratch_base(str(env["root"])) / "abcd1234"

    async def run():
        orch = await _orch(env, cwd=str(scratch), branch=None)
        drv = await _ensure(orch, env)
        await _close(orch, env["sid"])
        return drv

    assert asyncio.run(run()) is not None
    assert scratch.is_dir()


def test_preamble_is_prepended_once_to_the_next_prompt(env):
    sent: list[str] = []

    class _Drv:
        async def send_prompt(self, text, attachments, **kw):
            sent.append(text)

    async def run():
        orch = await _orch(env, cwd=env["cwd"])
        orch._drivers[env["sid"]] = _Drv()
        orch._queue_prompt_preamble(env["sid"], "NOTE: worktree was recreated")
        assert await orch.send_prompt(env["sid"], "hello") == "sent"
        assert await orch.send_prompt(env["sid"], "again") == "sent"
        orch._drivers.pop(env["sid"])
        return orch

    orch = asyncio.run(run())
    assert sent[0].startswith("NOTE: worktree was recreated")
    assert sent[0].endswith("hello")
    assert sent[1] == "again"
    assert orch._last_prompt[env["sid"]] == "again"


def test_begin_drain_snapshots_a_dirty_live_worktree(env):
    (Path(env["cwd"]) / "wip.txt").write_text("unsaved\n")

    async def run():
        orch = await _orch(env, cwd=env["cwd"])
        assert await _ensure(orch, env) is not None
        # The rearm-snapshot pass is not under test and globs the account's
        # transcript dirs; stub it so only the recovery pass runs.
        with patch.object(orch, "_snapshot_open_tasks", AsyncMock()):
            await orch.begin_drain(selfclear_sec=60)
        orch.cancel_drain()
        await _close(orch, env["sid"])

    asyncio.run(run())
    refs = worktrees.recovery_refs(str(env["clone"]), env["sid"])
    assert len(refs) == 1
    assert "wip.txt" in _git("ls-tree", "-r", "--name-only", refs[0], cwd=env["clone"])


def test_permanent_delete_of_dirty_archived_worktree_leaves_a_ref(env):
    (Path(env["cwd"]) / "wip.txt").write_text("unsaved\n")

    async def run():
        orch = await _orch(env, cwd=env["cwd"], status="archived")
        return await orch.permanent_delete(env["sid"])

    assert asyncio.run(run()) is True
    assert not Path(env["cwd"]).exists()
    refs = worktrees.recovery_refs(str(env["clone"]), env["sid"])
    assert len(refs) == 1
    assert "wip.txt" in _git("ls-tree", "-r", "--name-only", refs[0], cwd=env["clone"])
