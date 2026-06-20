"""Integration test: create_session/close_session drive the worktree lifecycle.

Uses the fake claude PTY (no real account, no quota) but the REAL orchestrator
code path, so it proves the wiring: a branch-selected session runs in an isolated
worktree, the user's real checkout is untouched, and close cleans the worktree up.
"""
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from ccswitch import worktrees
from server.app import create_app
from server.sessions.orchestrator import SessionOrchestrator

REPO_ROOT = Path(__file__).parent.parent.parent
FAKE_PTY = str(REPO_ROOT / "tests" / "fakes" / "fake_claude_pty.py")


def _git(*args: str, cwd: str) -> None:
    subprocess.run(["git", "-C", cwd, *args], check=True, capture_output=True)


def _make_repo(path: Path) -> None:
    subprocess.run(["git", "init", str(path), "-b", "main"], check=True, capture_output=True)
    _git("config", "user.email", "t@example.com", cwd=str(path))
    _git("config", "user.name", "Test", cwd=str(path))
    (path / "README.md").write_text("hi\n")
    _git("add", "-A", cwd=str(path))
    _git("commit", "-m", "init", cwd=str(path))
    _git("branch", "feature", cwd=str(path))


def _seed_account(name: str) -> None:
    import time as _time
    from ccswitch import paths
    from ccswitch.creds import write_json_secure
    write_json_secure(paths.slot_path(name), {
        "accessToken": f"{name}-tok", "refreshToken": f"{name}-ref",
        "expiresAt": int(_time.time() * 1000) + 9_000_000, "subscriptionType": "max",
    })
    if not paths.shared_mcp_path().exists():
        write_json_secure(paths.shared_mcp_path(), {})


def _fake_usage(account: str):
    from datetime import datetime, timedelta, timezone
    from ccswitch.oauth import Usage
    reset = datetime.now(timezone.utc) + timedelta(hours=5)
    return Usage(10.0, reset, 10.0, reset, False)


@pytest.fixture()
def wt_setup(tmp_path):
    store_home = tmp_path / "store"; store_home.mkdir()
    shared_base = tmp_path / "dot-claude"; shared_base.mkdir()
    (shared_base / "settings.json").write_text("{}")
    (shared_base / "projects").mkdir()
    cfg_dir = tmp_path / "cfg" / "alpha"; cfg_dir.mkdir(parents=True)

    repo_root = tmp_path / "reporoot"; repo_root.mkdir()
    repo = repo_root / "myrepo"
    _make_repo(repo)

    env_patch = patch.dict(os.environ, {
        "CCSWITCH_HOME": str(store_home),
        "CLAUDE_CONFIG_DIR": str(shared_base),
        "CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:",
    })
    usage_patch = patch("ccswitch.accounts.usage_for", side_effect=_fake_usage)
    compose_patch = patch("server.sessions.orchestrator.compose", return_value=cfg_dir)
    select_patch = patch("server.sessions.orchestrator.select_account", return_value="alpha")

    with env_patch, usage_patch, compose_patch, select_patch:
        _seed_account("alpha")
        orch = SessionOrchestrator(
            claude_bin=[sys.executable, FAKE_PTY],
            work_dir=str(REPO_ROOT),
            poll_interval=9999.0,
            usage_fn=_fake_usage,
            repo_root=str(repo_root),
        )
        app = create_app(orchestrator=orch)
        with TestClient(app, raise_server_exceptions=False) as client:
            yield orch, client, repo, repo_root


def _branch_of(path: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True,
    ).stdout.strip()


def test_branch_session_runs_in_isolated_worktree_and_cleans_up(wt_setup):
    orch, client, repo, repo_root = wt_setup

    rec = client.portal.call(
        lambda: orch.create_session("wt-test", cwd=str(repo), branch="feature")
    )

    # Session cwd is a ccswitch-managed worktree, checked out to `feature`.
    assert worktrees.is_managed_path(rec.cwd, str(repo_root))
    assert os.path.isdir(rec.cwd)
    assert _branch_of(rec.cwd) == "feature"

    # The user's real checkout was never switched off `main`.
    assert _branch_of(str(repo)) == "main"

    # Closing the session removes the (clean) worktree.
    client.portal.call(lambda: orch.close_session(rec.id))
    assert not os.path.isdir(rec.cwd)
