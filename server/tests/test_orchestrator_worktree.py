"""Integration test: create_session/close_session drive the worktree lifecycle.

Uses a fake SDK client (no real account, no quota) but the REAL orchestrator
code path, so it proves the wiring: a branch-selected session runs in an isolated
worktree, the user's real checkout is untouched, and close cleans the worktree up.
"""
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from ccswitch import worktrees
from ccswitch.selector import Picked
from server.app import create_app
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory

REPO_ROOT = Path(__file__).parent.parent.parent


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
    select_patch = patch("server.sessions.orchestrator.select_account", return_value=Picked("alpha"))

    with env_patch, usage_patch, compose_patch, select_patch:
        _seed_account("alpha")
        orch = SessionOrchestrator(
            sdk_factory=fake_sdk_factory(),
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


def test_create_session_api_forwards_identity_and_permission_mode(wt_setup):
    """POST /api/sessions must forward caller identity + permissionMode to the driver.

    Regression test for the bug where create_session was called without `identity`
    or `permission_mode`, causing every API-created driver to bind identity="system"
    and mode="interactive" regardless of what the caller supplied.  A WS
    permissionResponse from the real user identity would therefore never match,
    hanging interactive permission futures indefinitely.
    """
    orch, client, repo, _repo_root = wt_setup

    caller_identity = "alice@example.com"
    resp = client.post(
        "/api/sessions",
        json={"permissionMode": "plan"},
        headers={"Tailscale-User-Login": caller_identity},
    )
    assert resp.status_code == 201, resp.text

    session_id = resp.json()["id"]
    driver = orch.driver(session_id)
    assert driver is not None, "driver not found in orchestrator after create"
    assert driver.identity == caller_identity, (
        f"driver.identity={driver.identity!r}, expected {caller_identity!r}; "
        "identity was not forwarded from the API endpoint into create_session"
    )
    assert driver.mode == "plan", (
        f"driver.mode={driver.mode!r}, expected 'plan'; "
        "permissionMode was not forwarded from the API endpoint into create_session"
    )


def _registered_worktrees(repo: str) -> set[str]:
    out = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        capture_output=True, text=True,
    ).stdout
    return {
        os.path.abspath(line[len("worktree "):].strip())
        for line in out.splitlines() if line.startswith("worktree ")
    }


def test_archive_session_leaves_worktrees_intact(wt_setup):
    """archive_session is a pure status flip: it must not touch any git worktree
    registrations.  Both the archived session's worktree and a sibling's must
    remain registered after archiving.  Disk reclaim (and the #177-safe
    scope-deregister) happens only at permanent_delete time."""
    orch, client, repo, _repo_root = wt_setup

    a = client.portal.call(lambda: orch.create_session("arch-a", cwd=str(repo), branch="feature"))
    b = client.portal.call(lambda: orch.create_session("arch-b", cwd=str(repo), branch="feature"))

    before = _registered_worktrees(str(repo))
    assert os.path.abspath(a.cwd) in before
    assert os.path.abspath(b.cwd) in before

    client.portal.call(lambda: orch.archive_session(a.id))

    after = _registered_worktrees(str(repo))
    assert os.path.abspath(a.cwd) in after, \
        "archive_session must not remove the worktree (pure status flip — disk reclaim is at permanent_delete)"
    assert os.path.abspath(b.cwd) in after, \
        "archiving a session must not affect a sibling's worktree (#177)"


def test_permanent_delete_deregisters_own_worktree_but_not_sibling(wt_setup):
    """permanent_delete scope-deregisters ONLY the deleted session's worktree;
    a sibling's live registration must survive (regression for issue #177 in the
    archive path, which previously ran a repo-global `git worktree prune`)."""
    orch, client, repo, _repo_root = wt_setup

    a = client.portal.call(lambda: orch.create_session("perm-a", cwd=str(repo), branch="feature"))
    b = client.portal.call(lambda: orch.create_session("perm-b", cwd=str(repo), branch="feature"))

    before = _registered_worktrees(str(repo))
    assert os.path.abspath(a.cwd) in before
    assert os.path.abspath(b.cwd) in before

    client.portal.call(lambda: orch.permanent_delete(a.id))

    after = _registered_worktrees(str(repo))
    assert os.path.abspath(a.cwd) not in after, \
        "permanent_delete must clear the deleted session's worktree registration"
    assert os.path.abspath(b.cwd) in after, \
        "permanent_delete must not reap a sibling's live worktree (#177)"


def test_repo_session_without_branch_gets_own_detached_worktree(wt_setup):
    """A repo session with NO branch (e.g. from the bottom dispatch slot) must still
    run in its own isolated worktree — never the shared clone — and two such sessions
    on the same repo must get DIFFERENT working directories."""
    orch, client, repo, repo_root = wt_setup

    rec1 = client.portal.call(lambda: orch.create_session("nb-1", cwd=str(repo)))
    rec2 = client.portal.call(lambda: orch.create_session("nb-2", cwd=str(repo)))

    # Each runs in a ccswitch-managed worktree, NOT the shared repo/clone dir.
    assert worktrees.is_managed_path(rec1.cwd, str(repo_root))
    assert worktrees.is_managed_path(rec2.cwd, str(repo_root))
    assert rec1.cwd != str(repo) and rec2.cwd != str(repo)
    assert os.path.isdir(rec1.cwd) and os.path.isdir(rec2.cwd)
    # Two no-branch sessions on the same repo never collide.
    assert rec1.cwd != rec2.cwd
    # The user's real checkout was never switched.
    assert _branch_of(str(repo)) == "main"
