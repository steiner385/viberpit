"""Sessions expose which repo they are working on (repo label feature).

The dashboard's repo indicator is read LIVE from each session's working
directory: GET /api/sessions runs git in the cwd and reports the origin's
``owner/name`` (a scratch / non-git cwd reports repo=None → "Scratch"). The
stored ``repo`` on the DB row / in-memory record remains the create-time intent
and survives rehydrate, but the wire value reflects what's actually on disk.

These tests drive the wiring: create_session persists `repo` to the DB row AND
the in-memory record, GET /api/sessions surfaces the live git-derived repo, and
a rehydrated (post server-restart) session still remembers its stored repo.
"""
from __future__ import annotations

import os
import subprocess
import time as _time
from datetime import datetime
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from server.app import create_app
from server.db import db_session
from server.db.models import Session as DbSession
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory

_IDENT = {"Tailscale-User-Login": "repotest@example.com"}


def _fake_usage(account: str):
    from datetime import timedelta, timezone
    from ccswitch.oauth import Usage
    reset = datetime.now(timezone.utc) + timedelta(hours=5)
    return Usage(10.0, reset, 10.0, reset, False)


def _seed_account(name: str) -> None:
    from ccswitch import paths
    from ccswitch.creds import write_json_secure
    write_json_secure(paths.slot_path(name), {
        "accessToken": f"{name}-tok", "refreshToken": f"{name}-ref",
        "expiresAt": int(_time.time() * 1000) + 9_000_000, "subscriptionType": "max",
    })
    if not paths.shared_mcp_path().exists():
        write_json_secure(paths.shared_mcp_path(), {})


@pytest.fixture()
def repo_setup(tmp_path):
    store_home = tmp_path / "store"; store_home.mkdir()
    shared_base = tmp_path / "dot-claude"; shared_base.mkdir()
    (shared_base / "settings.json").write_text("{}")
    (shared_base / "projects").mkdir()
    cfg_dir = tmp_path / "cfg" / "alpha"; cfg_dir.mkdir(parents=True)
    work = tmp_path / "work"; work.mkdir()
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"

    env_patch = patch.dict(os.environ, {
        "CCSWITCH_HOME": str(store_home),
        "CLAUDE_CONFIG_DIR": str(shared_base),
        "CCSWITCH_DB_URL": db_url,
        # Hermetic auth: pin the allowlist to the test identity so the test passes
        # regardless of the developer's real shell env (an empty allowlist is
        # rejected on a non-loopback bind).
        "CCSWITCH_TAILNET_ALLOWLIST": _IDENT["Tailscale-User-Login"],
    })
    with env_patch, \
            patch("ccswitch.accounts.usage_for", side_effect=_fake_usage), \
            patch("server.sessions.orchestrator.compose", return_value=cfg_dir), \
            patch("server.sessions.orchestrator.configdir"), \
            patch("server.sessions.orchestrator.select_account", return_value="alpha"):
        _seed_account("alpha")
        orch = SessionOrchestrator(
            sdk_factory=fake_sdk_factory(),
            poll_interval=9999.0,
            usage_fn=_fake_usage,
            repo_root=str(tmp_path / "reporoot"),
        )
        app = create_app(orchestrator=orch)
        with TestClient(app, raise_server_exceptions=False) as client:
            yield orch, client, str(work), db_url


def _init_git_repo(path: str, origin: str) -> None:
    """Make ``path`` a git working tree (with one commit) and an ``origin`` remote.

    A session on a real repo runs in a detached worktree, which requires a
    commit to detach from — so seed an empty initial commit. The worktree shares
    the repo's ``origin``, so the live repo read resolves through it.
    """
    def _git(*args: str) -> None:
        subprocess.run(
            ["git", "-C", path,
             "-c", "user.email=t@example.com", "-c", "user.name=test", *args],
            check=True, capture_output=True,
        )

    _git("init", "-q")
    _git("commit", "-q", "--allow-empty", "-m", "init")
    _git("remote", "add", "origin", origin)


def test_create_session_with_repo_is_surfaced_in_list(repo_setup):
    """GET /api/sessions reports the repo read live from the session's cwd."""
    orch, client, work, _db_url = repo_setup
    _init_git_repo(work, "https://github.com/acme/widget.git")

    rec = client.portal.call(
        lambda: orch.create_session("repo-session", cwd=work, repo="acme/widget")
    )
    assert rec.repo == "acme/widget", f"record.repo={rec.repo!r}"

    resp = client.get("/api/sessions", headers=_IDENT)
    assert resp.status_code == 200, resp.text
    sess = next(s for s in resp.json() if s["id"] == rec.id)
    assert sess["repo"] == "acme/widget", f"wire repo={sess.get('repo')!r}"


def test_list_repo_is_read_live_and_overrides_stale_stored_value(repo_setup):
    """The wire repo comes from the cwd's git origin, even if the stored value differs."""
    orch, client, work, _db_url = repo_setup
    # On-disk reality (origin) disagrees with the create-time stored repo.
    _init_git_repo(work, "git@github.com:acme/actual.git")

    rec = client.portal.call(
        lambda: orch.create_session("drifted", cwd=work, repo="acme/stale")
    )
    assert rec.repo == "acme/stale"  # stored intent unchanged

    resp = client.get("/api/sessions", headers=_IDENT)
    sess = next(s for s in resp.json() if s["id"] == rec.id)
    assert sess["repo"] == "acme/actual", f"wire repo={sess.get('repo')!r}"


def test_create_session_without_repo_reports_none(repo_setup):
    """A scratch/arbitrary-cwd session has no repo → repo is None in the API."""
    orch, client, work, _db_url = repo_setup

    rec = client.portal.call(lambda: orch.create_session("scratch-session", cwd=work))
    assert rec.repo is None

    resp = client.get("/api/sessions", headers=_IDENT)
    sess = next(s for s in resp.json() if s["id"] == rec.id)
    assert sess["repo"] is None, f"wire repo={sess.get('repo')!r}"


def test_pr_badge_uses_live_worktree_branch_not_stale_stored(repo_setup):
    """GET /api/sessions joins the PR by the worktree's CURRENT branch.

    A ccswitch session is created on a per-session isolation branch, then the
    agent checks out a real ``feat/*`` branch (the PR head) in the same worktree.
    The PR cache is keyed only by that live branch — so the badge appears only if
    the list route resolves the branch live rather than using the stored value.
    """
    orch, client, work, _db_url = repo_setup
    _init_git_repo(work, "https://github.com/acme/widget.git")

    rec = client.portal.call(
        lambda: orch.create_session("pr-session", cwd=work, repo="acme/widget")
    )
    # The agent moves the worktree onto the real PR branch after creation.
    subprocess.run(
        ["git", "-C", rec.cwd, "checkout", "-q", "-b", "feat/real-pr"],
        check=True, capture_output=True,
    )
    # The poller has only ever cached the PR under its live head branch.
    from server.github.pr_status import PullRequestInfo
    orch._pr_poller._cache[("acme/widget", "feat/real-pr")] = PullRequestInfo(
        220, "https://github.com/acme/widget/pull/220", "open", "passing", "t"
    )

    resp = client.get("/api/sessions", headers=_IDENT)
    sess = next(s for s in resp.json() if s["id"] == rec.id)
    assert sess["pr"] is not None, f"pr missing; stored branch was {rec.branch!r}"
    assert sess["pr"]["number"] == 220


def test_repo_persists_to_db_and_survives_rehydrate(repo_setup):
    """The repo is written to the DB row and restored by rehydrate() after a restart."""
    orch, client, work, db_url = repo_setup

    rec = client.portal.call(
        lambda: orch.create_session("durable", cwd=work, repo="acme/durable")
    )

    async def _read_row():
        from sqlalchemy import select as sa_select
        async with db_session() as db:
            row = (await db.execute(
                sa_select(DbSession).where(DbSession.id == rec.id)
            )).scalar_one()
            return row.repo

    assert client.portal.call(_read_row) == "acme/durable"

    # Simulate a restart: a fresh orchestrator rehydrates from the same DB.
    fresh = SessionOrchestrator(
        sdk_factory=fake_sdk_factory(), poll_interval=9999.0, usage_fn=_fake_usage,
    )
    client.portal.call(fresh.rehydrate)
    restored = fresh.get_session(rec.id)
    assert restored is not None
    assert restored.repo == "acme/durable", f"rehydrated repo={restored.repo!r}"
