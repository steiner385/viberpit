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
import server.sessions.orchestrator as orch_mod
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


async def _orch(env, *, cwd: str, branch: str | None = "feature", status: str = "idle",
                host: str | None = None):
    await init_db(db_url=env["db_url"])
    async with db_session() as db:
        db.add(DbSession(id=env["sid"], name="t", account_name="acct1",
                         engine_session_id="csid", status=status, cwd=cwd,
                         repo=REPO, branch=branch, host=host))
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


def _events(caplog, name):
    return [r for r in caplog.records if r.getMessage() == name]


def test_empty_managed_dir_is_recreated_in_place(env):
    """The literal #2913 state: the old mkdir left an EMPTY dir at the path."""
    shutil.rmtree(env["cwd"])
    Path(env["cwd"]).mkdir()

    async def run():
        orch = await _orch(env, cwd=env["cwd"])
        drv = await _ensure(orch, env)
        await _close(orch, env["sid"])
        return orch, drv

    orch, drv = asyncio.run(run())
    assert drv is not None
    assert worktrees.current_branch(env["cwd"]) == "feature"
    assert env["sid"] in orch._pending_prompt_preambles


def test_non_empty_gitless_managed_dir_is_refused_untouched(env, caplog):
    shutil.rmtree(env["cwd"])
    Path(env["cwd"]).mkdir()
    stray = Path(env["cwd"]) / "stray.txt"
    stray.write_text("whatever this is, it is not ours to judge\n")

    async def run():
        orch = await _orch(env, cwd=env["cwd"])
        return await _ensure(orch, env)

    with caplog.at_level("INFO"):
        assert asyncio.run(run()) is None
    assert stray.read_text().startswith("whatever")
    assert sorted(p.name for p in Path(env["cwd"]).iterdir()) == ["stray.txt"]
    assert _events(caplog, "worktree.missing")


def test_foreign_host_row_is_never_recreated(env, caplog):
    shutil.rmtree(env["cwd"])

    async def run():
        orch = await _orch(env, cwd=env["cwd"], host="some-other-host")
        return await _ensure(orch, env)

    with caplog.at_level("INFO"):
        assert asyncio.run(run()) is None
    assert not Path(env["cwd"]).exists()
    missing = _events(caplog, "worktree.missing")
    assert missing and missing[0].fields["host"] == "some-other-host"


def test_worktree_missing_warns_once_then_info(env, caplog):
    shutil.rmtree(env["cwd"])

    async def run():
        orch = await _orch(env, cwd=env["cwd"], branch="gone-branch")
        assert await _ensure(orch, env) is None
        assert await _ensure(orch, env) is None

    with caplog.at_level("INFO"):
        asyncio.run(run())
    levels = [r.levelname for r in _events(caplog, "worktree.missing")]
    assert levels == ["WARNING", "INFO"]


@pytest.mark.asyncio
async def test_reassign_refusal_parks_terminally(env, monkeypatch):
    """A relaunch over a missing, unrecoverable worktree must not land in the
    retryable pause the stuck-session sweep re-enters every 300 s."""
    shutil.rmtree(env["cwd"])
    orch = await _orch(env, cwd=env["cwd"], branch="gone-branch")
    paused = []

    async def spy_pause(record, reason, earliest_reset=None):
        paused.append(reason)

    orch._pause_session = spy_pause
    monkeypatch.setattr(orch_mod, "compose", lambda acct: env["cfg"])
    monkeypatch.setattr(orch_mod.configdir, "trust_project", lambda *a, **k: None)
    rec = orch._records[env["sid"]]
    outcome = await orch._reassign(rec, "acct1", reason="stuck_session_sweep")
    assert outcome == "failed"
    assert paused == ["worktree_missing"]
    assert "worktree_missing" in orch_mod._TERMINAL_PAUSE_REASONS
    assert "worktree_missing" in orch_mod.PAUSE_REASONS
    assert not Path(env["cwd"]).exists()


@pytest.mark.asyncio
async def test_adopt_refuses_a_missing_managed_cwd(env, monkeypatch):
    from types import SimpleNamespace
    shutil.rmtree(env["cwd"])
    await init_db(db_url=env["db_url"])
    orch = SessionOrchestrator(sdk_factory=_factory, repo_root=str(env["root"]))
    cand = SimpleNamespace(cwd=env["cwd"], mapped=True, tracked=False, pid=None,
                           account="acct1")
    monkeypatch.setattr(orch, "_resolve_candidate", lambda csid: cand)
    with pytest.raises(KeyError):
        await orch.adopt_session("claude-sid", identity="u@example.com")
    assert not Path(env["cwd"]).exists()


def test_drain_publishes_and_arms_before_snapshotting_and_survives_cancel(env):
    """The snapshot pass may take seconds: the UI hold and the self-clear timer must
    already be in place, and a cancel_drain landing mid-pass must leave the
    orchestrator cleanly idle."""
    (Path(env["cwd"]) / "wip.txt").write_text("unsaved\n")
    seen: dict = {}

    async def run():
        orch = await _orch(env, cwd=env["cwd"])
        assert await _ensure(orch, env) is not None
        events = []
        orch._publish_event = lambda e: events.append(e["type"])
        real = worktrees.snapshot_recovery_ref

        def slow_snapshot(*a, **kw):
            seen["events_at_snapshot"] = list(events)
            seen["timer_armed"] = orch._drain_timer is not None
            orch._loop_ref.call_soon_threadsafe(orch.cancel_drain)
            import time as _t
            _t.sleep(0.3)
            return real(*a, **kw)

        orch._loop_ref = asyncio.get_running_loop()
        with patch.object(orch, "_snapshot_open_tasks", AsyncMock()), \
             patch.object(worktrees, "snapshot_recovery_ref", slow_snapshot):
            status = await orch.begin_drain(selfclear_sec=60)
        seen["status"] = status
        seen["timer"] = orch._drain_timer
        await _close(orch, env["sid"])

    asyncio.run(run())
    assert seen["events_at_snapshot"] == ["updatePending"]
    assert seen["timer_armed"]
    assert seen["status"]["state"] == "idle"
    assert seen["timer"] is None


def test_drain_snapshot_stops_starting_targets_after_its_deadline(env, monkeypatch):
    """The worker thread must not outlive the budget by walking the rest."""
    calls = []

    def slow_snapshot(cwd, sid, *, reason):
        calls.append(sid)
        import time as _t
        _t.sleep(1.5)
        return None

    async def run():
        orch = await _orch(env, cwd=env["cwd"])
        assert await _ensure(orch, env) is not None
        # Three live sessions sharing the one managed worktree path.
        for extra in ("x1", "x2"):
            orch._drivers[extra] = orch._drivers[env["sid"]]
            orch._records[extra] = orch._records[env["sid"]]
        monkeypatch.setattr(orch_mod, "DRAIN_RECOVERY_SNAPSHOT_S", 1.0)
        with patch.object(worktrees, "snapshot_recovery_ref", slow_snapshot):
            await orch._snapshot_recovery_refs_at_drain()
            await asyncio.sleep(3.5)       # let a still-running thread show itself
        for extra in ("x1", "x2"):
            orch._drivers.pop(extra)
            orch._records.pop(extra)
        await _close(orch, env["sid"])

    asyncio.run(run())
    assert len(calls) == 1, f"targets started past the deadline: {calls}"


def test_drain_judges_managed_paths_against_the_effective_root(env):
    """Worktrees are created under the settings root; the drain must see them even
    when the constructor root differs (one _managed_roots for both callers)."""
    (Path(env["cwd"]) / "wip.txt").write_text("unsaved\n")

    async def run():
        orch = await _orch(env, cwd=env["cwd"])
        assert await _ensure(orch, env) is not None
        root = str(env["root"])
        orch._repo_root = "/nonexistent-constructor-root"

        async def eff():
            return root

        orch._effective_repo_root = eff
        with patch.object(orch, "_snapshot_open_tasks", AsyncMock()):
            await orch.begin_drain(selfclear_sec=60)
        orch.cancel_drain()
        await _close(orch, env["sid"])

    asyncio.run(run())
    assert worktrees.recovery_refs(str(env["clone"]), env["sid"])
