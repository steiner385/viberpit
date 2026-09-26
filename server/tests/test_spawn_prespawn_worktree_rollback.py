"""#2255: a spawn that fails AFTER its worktree exists must not leak it.

`_spawn_session` checks out the per-session worktree before select_account(),
the Copilot/Codex starts and the display lease — all of which run before the
driver rollback owns the spawn. A leaked worktree keeps its deterministic child
branch (`<prefix><base>-<short id>`), so every retry of the same session id died
on "a branch named ... already exists" instead of the real error. On dobby this
turned one transient AllAccountsBusy on a model approval into 13 permanent 409s.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from viberpit.selector import AllAccountsBusy
from server.sessions.display_policy import DISPLAY_VIRTUAL
from server.sessions.orchestrator import SessionOrchestrator


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout


def _repo_with_origin(tmp_path: Path) -> Path:
    """A clone whose `main` lives on origin — the child-branch path of create_worktree."""
    origin = tmp_path / "origin.git"
    _git("init", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    clone = tmp_path / "KinDash"
    _git("clone", str(origin), str(clone), cwd=tmp_path)
    _git("-c", "user.name=t", "-c", "user.email=t@example.com",
         "commit", "--allow-empty", "-m", "init", cwd=clone)
    _git("push", "origin", "HEAD:main", cwd=clone)
    return clone


class _X11:
    def __init__(self) -> None:
        self.started: list[str] = []
        self.stopped: list[str] = []

    async def start_display(self, session_id: str) -> None:
        self.started.append(session_id)

    async def stop_display(self, session_id: str) -> None:
        self.stopped.append(session_id)


async def _spawn(orch: SessionOrchestrator, session_id: str, clone: Path) -> None:
    await orch._spawn_session(
        session_id, name="s", account=None, initial_prompt=None, cols=80, rows=24,
        cwd=str(clone), branch="main", repo=None, identity="person@example.com",
        permission_mode="interactive", attachment_ids=None, name_is_auto=False,
        origin=None, origin_ref=None, model=None,
    )


@pytest.mark.asyncio
async def test_all_accounts_busy_after_worktree_releases_it_so_a_retry_can_spawn(
    tmp_path: Path,
) -> None:
    clone = _repo_with_origin(tmp_path)
    repo_root = tmp_path / "repos"
    repo_root.mkdir()
    orch = SessionOrchestrator(repo_root=str(repo_root))
    session_id = "d1c7eb8c-9a0b-4cfb-9789-8dd58f8819b4"
    busy = AllAccountsBusy(None)

    with patch("server.sessions.orchestrator.select_account", side_effect=busy):
        with pytest.raises(AllAccountsBusy):
            await _spawn(orch, session_id, clone)
        # Before the fix the retry raised BranchSwitchError("a branch named
        # 'viberpit/main-d1c7eb8c' already exists") — the real error was masked.
        with pytest.raises(AllAccountsBusy):
            await _spawn(orch, session_id, clone)

    assert session_id not in orch._worktrees
    assert _git("branch", "--list", "*main-d1c7eb8c", cwd=clone).strip() == ""
    worktree_paths = _git("worktree", "list", "--porcelain", cwd=clone)
    assert "d1c7eb8c" not in worktree_paths


@pytest.mark.asyncio
async def test_failure_after_display_lease_releases_display_and_worktree(
    tmp_path: Path,
) -> None:
    """A raise after the virtual display is leased releases the lease too."""
    clone = _repo_with_origin(tmp_path)
    repo_root = tmp_path / "repos"
    repo_root.mkdir()
    x11 = _X11()
    orch = SessionOrchestrator(repo_root=str(repo_root), x11=x11)
    session_id = "0badc0de-0000-0000-0000-000000000000"

    with (
        patch.object(orch, "_resolve_display_mode_for", return_value=DISPLAY_VIRTUAL),
        patch("server.sessions.orchestrator.select_account") as pick,
        patch.object(orch, "_driver_config_dir", return_value=str(tmp_path)),
        patch("server.sessions.orchestrator.local_host_stamp",
              side_effect=RuntimeError("boom")),
    ):
        pick.return_value.name = "acct"
        pick.return_value.optimistic = False
        with pytest.raises(RuntimeError, match="boom"):
            await _spawn(orch, session_id, clone)

    assert x11.started == [session_id]
    assert x11.stopped == [session_id]
    assert session_id not in orch._worktrees
    assert session_id not in orch._records
    assert _git("branch", "--list", "*main-0badc0de", cwd=clone).strip() == ""
