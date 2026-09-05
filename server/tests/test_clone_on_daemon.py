"""Regression tests: repo→clone resolution must run on the DAEMON, not the edge.

Background (production incident): the session daemon runs on a different host
(rocky) from the web edge (dobby). ``server/api/sessions.py`` used to resolve
``repo=`` into a ccswitch-owned clone path (``gitrepos.ensure_clone``) itself,
in the EDGE process, then pass the resulting ``cwd`` to the daemon over RPC.
Cross-host, the clone landed on the edge host's filesystem — the daemon then
rejected the session with "cwd does not exist" because it has no such
directory. ``gitrepos.ensure_clone`` and its registry are daemon-owned (see
``server/sessions/remote.py`` and ``server/api/repos.py``); the fix moves
resolution into ``SessionOrchestrator.create_session`` so it always runs in
the process that actually owns the clone namespace — the daemon in dual-host
mode, and (identically) the edge-as-daemon in single-host/in-process mode.

These tests cover:
  1. The orchestrator resolves ``cwd`` from ``repo`` via ``gitrepos.ensure_clone``
     when ``cwd`` is not supplied, using its own effective repo root.
  2. An explicitly-supplied non-existent ``cwd`` still raises ValueError — the
     existing guard is not weakened, and repo resolution never runs when cwd
     is given.
  3. A clone failure (CloneError/GhError) propagates as itself in-process, and
     survives the daemon RPC boundary via the same typed-reconstruction
     mechanism as AllAccountsBusy — so the edge's existing 502 clone_failed
     handling keeps working under both hosting modes.
  4. The edge (``server/api/sessions.py``) no longer calls ``ensure_clone``
     itself — a tripwire patched onto the edge module's own ``gitrepos``
     binding (leaving the real module used by the orchestrator untouched)
     must never fire for a successful repo-backed create.
"""
from __future__ import annotations

import os
import subprocess
import time as _time
import types
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from ccswitch import gitrepos
from ccswitch.selector import Picked
from server.ipc.client import DaemonError
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.remote import RemoteOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory

IDENTITY_HEADERS = {"Tailscale-User-Login": "clonetest@example.com"}


def _fake_usage(account: str):
    from datetime import timedelta
    from ccswitch.quota import anthropic_snapshot
    reset = datetime.now(timezone.utc) + timedelta(hours=5)
    return anthropic_snapshot(10.0, reset, 10.0, reset, False)


def _seed_account(name: str) -> None:
    from ccswitch import paths
    from ccswitch.creds import write_json_secure
    write_json_secure(paths.slot_path(name), {
        "accessToken": f"{name}-tok", "refreshToken": f"{name}-ref",
        "expiresAt": int(_time.time() * 1000) + 9_000_000, "subscriptionType": "max",
    })
    if not paths.shared_mcp_path().exists():
        write_json_secure(paths.shared_mcp_path(), {})


def _init_bare_clone(path) -> None:
    """A real, minimal git working tree with one commit — enough for
    ``worktrees.create_worktree`` (detach-off-HEAD) to succeed against it."""
    path.mkdir(parents=True, exist_ok=True)

    def _git(*args: str) -> None:
        subprocess.run(
            ["git", "-C", str(path),
             "-c", "user.email=t@example.com", "-c", "user.name=test", *args],
            check=True, capture_output=True,
        )
    _git("init", "-q", "-b", "main")
    _git("commit", "-q", "--allow-empty", "-m", "init")


@pytest.fixture()
def orch_setup(tmp_path):
    store_home = tmp_path / "store"; store_home.mkdir()
    shared_base = tmp_path / "dot-claude"; shared_base.mkdir()
    (shared_base / "settings.json").write_text("{}")
    (shared_base / "projects").mkdir()
    cfg_dir = tmp_path / "cfg" / "alpha"; cfg_dir.mkdir(parents=True)
    repo_root = tmp_path / "reporoot"; repo_root.mkdir()
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"

    env_patch = patch.dict(os.environ, {
        "CCSWITCH_HOME": str(store_home),
        "CLAUDE_CONFIG_DIR": str(shared_base),
        "CCSWITCH_DB_URL": db_url,
        "CCSWITCH_TAILNET_ALLOWLIST": IDENTITY_HEADERS["Tailscale-User-Login"],
    })
    with env_patch, \
            patch("ccswitch.accounts.usage_for", side_effect=_fake_usage), \
            patch("server.sessions.orchestrator.compose", return_value=cfg_dir), \
            patch("server.sessions.orchestrator.configdir"), \
            patch("server.sessions.orchestrator.select_account", return_value=Picked("alpha")):
        _seed_account("alpha")
        orch = SessionOrchestrator(
            sdk_factory=fake_sdk_factory(),
            poll_interval=9999.0,
            usage_fn=_fake_usage,
            repo_root=str(repo_root),
        )
        yield orch, str(repo_root), db_url


# ---------------------------------------------------------------------------
# 1. The orchestrator resolves cwd from repo via ensure_clone.
# ---------------------------------------------------------------------------


def test_orchestrator_resolves_cwd_from_repo(orch_setup, tmp_path):
    orch, repo_root, db_url = orch_setup
    clone_dir = tmp_path / "clones" / "acme" / "widget"
    _init_bare_clone(clone_dir)

    captured: dict = {}

    def fake_ensure_clone(rr, name_with_owner, url):
        captured["repo_root"] = rr
        captured["name_with_owner"] = name_with_owner
        captured["url"] = url
        return str(clone_dir)

    from server.db import init_db, dispose_db
    import asyncio

    async def _run():
        await init_db(db_url=db_url)
        await orch.start()
        try:
            with patch("ccswitch.gitrepos.ensure_clone", side_effect=fake_ensure_clone):
                return await orch.create_session("clone-test", repo="acme/widget")
        finally:
            await orch.stop()
            await dispose_db()

    rec = asyncio.run(_run())

    assert captured["name_with_owner"] == "acme/widget"
    assert captured["url"] == "https://github.com/acme/widget.git"
    assert captured["repo_root"] == repo_root, (
        f"ensure_clone called with repo_root={captured['repo_root']!r}, "
        f"expected the orchestrator's effective repo root {repo_root!r}"
    )
    # The clone flows through into the session's actual working directory
    # (via the existing per-session worktree machinery).
    assert rec.cwd, "session cwd was not set from the resolved clone"
    assert os.path.isdir(rec.cwd)
    assert rec.repo == "acme/widget"


# ---------------------------------------------------------------------------
# 2. Explicit cwd that doesn't exist still raises ValueError; repo is ignored.
# ---------------------------------------------------------------------------


def test_explicit_missing_cwd_still_raises_and_skips_repo_resolution(orch_setup, tmp_path):
    orch, _repo_root, _db_url = orch_setup
    missing = str(tmp_path / "does" / "not" / "exist")

    import asyncio

    async def _run():
        # No init_db needed: the cwd-must-exist guard raises before any DB or
        # ensure_clone access, so this exercises that ordering directly.
        with patch("ccswitch.gitrepos.ensure_clone") as mock_ensure:
            with pytest.raises(ValueError, match="cwd does not exist"):
                await orch.create_session("bad-cwd", cwd=missing, repo="acme/widget")
            mock_ensure.assert_not_called()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# 3a. Clone failure propagates in-process as the real CloneError/GhError.
# ---------------------------------------------------------------------------


def test_clone_failure_propagates_in_process(orch_setup):
    orch, _repo_root, _db_url = orch_setup

    import asyncio

    async def _run():
        # No init_db needed: ensure_clone raises before create_session touches
        # the DB (session_id/DB row creation happens after cwd resolution).
        with patch("ccswitch.gitrepos.ensure_clone",
                   side_effect=gitrepos.CloneError("boom")):
            with pytest.raises(gitrepos.CloneError):
                await orch.create_session("clone-fail", repo="acme/widget")

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# 3b. Clone failure survives the daemon RPC boundary (mirrors AllAccountsBusy).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remote_create_session_reraises_clone_error():
    class _BoomClient:
        async def call(self, method, **params):
            raise DaemonError("CloneError", "boom")

    ro = RemoteOrchestrator(_BoomClient())
    with pytest.raises(gitrepos.CloneError, match="boom"):
        await ro.create_session(name="x", repo="acme/widget")


@pytest.mark.asyncio
async def test_remote_create_session_reraises_gh_error():
    class _BoomClient:
        async def call(self, method, **params):
            raise DaemonError("GhError", "gh not authed")

    ro = RemoteOrchestrator(_BoomClient())
    with pytest.raises(gitrepos.GhError, match="gh not authed"):
        await ro.create_session(name="x", repo="acme/widget")


# ---------------------------------------------------------------------------
# 3c. API layer still returns 502 clone_failed under the new resolution path.
# ---------------------------------------------------------------------------


def test_api_create_session_clone_error_still_502(tmp_path):
    """Belt-and-suspenders on top of test_repos.py's existing coverage: a
    CloneError raised by the (now orchestrator-driven) ensure_clone call must
    still surface as HTTP 502 {"error": {"code": "clone_failed"}}."""
    from tests.helpers import temp_env
    with temp_env():
        with patch("ccswitch.gitrepos.ensure_clone",
                   side_effect=gitrepos.CloneError("boom")), \
             patch.dict(os.environ, {
                 "CCSWITCH_REPO_ROOT": str(tmp_path),
                 "CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:",
             }):
            from server.app import create_app
            from fastapi.testclient import TestClient
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0)
            app = create_app(orchestrator=orch)
            client = TestClient(app, raise_server_exceptions=False)
            with client:
                resp = client.post(
                    "/api/sessions",
                    json={"name": "test", "repo": "acme/widget"},
                    headers=IDENTITY_HEADERS,
                )
    assert resp.status_code == 502, resp.text
    body = resp.json()
    assert body["error"]["code"] == "clone_failed"


# ---------------------------------------------------------------------------
# 4. Edge no longer calls ensure_clone itself — tripwire on the edge module's
#    own `gitrepos` binding, isolated from the real module the orchestrator
#    (in-process here, i.e. running as its own daemon) actually uses.
# ---------------------------------------------------------------------------


def test_api_create_session_never_calls_ensure_clone_itself(tmp_path):
    from tests.helpers import temp_env
    from ccswitch import paths

    clone_dir = tmp_path / ".ccswitch" / "clones" / "acme" / "widget"
    _init_bare_clone(clone_dir)

    real_calls: list = []

    def working_fake_ensure_clone(repo_root, name_with_owner, url):
        real_calls.append((repo_root, name_with_owner, url))
        return str(clone_dir)

    # A stand-in for the `gitrepos` name as seen from inside server/api/sessions.py
    # ONLY. It carries the real exception types (so the existing except clause
    # still matches whatever the orchestrator raises) and delegates
    # repo_at_path (used by _resolve_repo after create) to the real
    # implementation, but ensure_clone is a tripwire: if server/api/sessions.py
    # still called it directly (the bug being fixed), this fires and fails the
    # test. The real `ccswitch.gitrepos` module — and hence the orchestrator's
    # own `gitrepos.ensure_clone` call, which is a SEPARATE name binding in
    # server/sessions/orchestrator.py's namespace — is untouched by this patch.
    edge_gitrepos_stub = types.SimpleNamespace(
        CloneError=gitrepos.CloneError,
        GhError=gitrepos.GhError,
        repo_at_path=gitrepos.repo_at_path,
        ensure_clone=lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("edge called gitrepos.ensure_clone directly — "
                            "clone resolution must happen on the daemon/orchestrator")),
    )

    with temp_env():
        store = paths.store_dir()
        store.mkdir(parents=True, exist_ok=True)
        now_ms = int(_time.time() * 1000)
        slot = store / "account1.cred.json"
        slot.write_text(
            f'{{"accessToken":"tok","refreshToken":"ref",'
            f'"expiresAt":{now_ms + 9_000_000},"subscriptionType":"max"}}')
        cfg_dir = paths.account_cfg_dir("account1")
        cfg_dir.mkdir(parents=True, exist_ok=True)

        with patch("server.sessions.orchestrator.select_account", return_value=Picked("account1")), \
             patch("server.sessions.orchestrator.compose", return_value=cfg_dir), \
             patch("ccswitch.gitrepos.ensure_clone", side_effect=working_fake_ensure_clone), \
             patch("server.api.sessions.gitrepos", edge_gitrepos_stub), \
             patch.dict(os.environ, {
                 "CCSWITCH_REPO_ROOT": str(tmp_path),
                 "CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:",
             }):
            from server.sessions.orchestrator import SessionOrchestrator as _SO
            from server.app import create_app
            from fastapi.testclient import TestClient
            orch = _SO(sdk_factory=fake_sdk_factory(), poll_interval=9999.0)
            app = create_app(orchestrator=orch)
            client = TestClient(app, raise_server_exceptions=False)
            with client:
                resp = client.post(
                    "/api/sessions",
                    json={"name": "test", "repo": "acme/widget"},
                    headers=IDENTITY_HEADERS,
                )

    assert resp.status_code == 201, resp.text
    # The real (orchestrator-side) ensure_clone was the one actually called.
    assert real_calls and real_calls[0][1] == "acme/widget"
