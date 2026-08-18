"""Tests for /api/github/* and /api/config endpoints, plus session repo-clone."""
from __future__ import annotations
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from ccswitch.selector import Picked
from tests.helpers import temp_env
from server.tests.fakes.fake_factory import fake_sdk_factory

IDENTITY_HEADERS = {"Tailscale-User-Login": "testuser@example.com"}


def _make_app_and_client():
    from server.sessions.orchestrator import SessionOrchestrator
    from server.app import create_app
    with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0)
        app = create_app(orchestrator=orch)
        from fastapi.testclient import TestClient
        client = TestClient(app, raise_server_exceptions=False)
        return app, client


# ---------------------------------------------------------------------------
# GET /api/github/repos
# ---------------------------------------------------------------------------

_REPOS = [
    {
        "nameWithOwner": "cairnea/KinDash",
        "name": "KinDash",
        "owner": "cairnea",
        "isPrivate": True,
        "url": "https://github.com/cairnea/KinDash",
        "defaultBranch": "main",
        "pushedAt": "2026-06-20T00:00:00Z",
    },
    {
        "nameWithOwner": "acme/widget",
        "name": "widget",
        "owner": "acme",
        "isPrivate": False,
        "url": "https://github.com/acme/widget",
        "defaultBranch": "main",
        "pushedAt": "2026-06-19T00:00:00Z",
    },
]


def test_list_github_repos_annotates_cloned(tmp_path):
    """GET /api/github/repos returns repos with a 'cloned' flag, order preserved."""
    # Pretend cairnea/KinDash is already cloned under <root>/.ccswitch/clones.
    clone_git = tmp_path / ".ccswitch" / "clones" / "cairnea" / "KinDash" / ".git"
    clone_git.mkdir(parents=True)

    with temp_env():
        with patch.dict(os.environ, {"CCSWITCH_REPO_ROOT": str(tmp_path), "CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            with patch("ccswitch.gitrepos.list_accessible_repos", return_value=[dict(r) for r in _REPOS]):
                _, client = _make_app_and_client()
                with client:
                    resp = client.get("/api/github/repos", headers=IDENTITY_HEADERS)

    assert resp.status_code == 200
    data = resp.json()
    # Order preserved (pushedAt-desc as returned by gitrepos).
    assert [r["nameWithOwner"] for r in data] == ["cairnea/KinDash", "acme/widget"]
    assert data[0]["cloned"] is True
    assert data[1]["cloned"] is False
    assert data[0]["isPrivate"] is True


def test_list_github_repos_gh_error_502(tmp_path):
    """GhError with no cached list (cold) surfaces as HTTP 502."""
    from ccswitch.gitrepos import GhError
    with temp_env():
        with patch.dict(os.environ, {"CCSWITCH_REPO_ROOT": str(tmp_path), "CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            with patch("ccswitch.gitrepos.list_accessible_repos", side_effect=GhError("not logged in")):
                _, client = _make_app_and_client()
                with client:
                    resp = client.get("/api/github/repos", headers=IDENTITY_HEADERS)
    assert resp.status_code == 502


def test_list_github_repos_serves_cached_on_gh_error(tmp_path):
    """A GitHub failure (e.g. rate limit) after a prior success serves the last-good
    list as 200 (stale) instead of a 502 — so the browser doesn't enter a retry storm.
    Regression for the GraphQL-rate-limit "UI freaking out" incident."""
    from ccswitch.gitrepos import GhError
    with temp_env():
        with patch.dict(os.environ, {"CCSWITCH_REPO_ROOT": str(tmp_path), "CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            _, client = _make_app_and_client()
            with client:
                # 1. prime the per-app cache with a good response
                with patch("ccswitch.gitrepos.list_accessible_repos",
                           return_value=[dict(r) for r in _REPOS]):
                    ok = client.get("/api/github/repos", headers=IDENTITY_HEADERS)
                assert ok.status_code == 200
                # 2. GitHub now fails — must serve the cached list, not 502
                with patch("ccswitch.gitrepos.list_accessible_repos",
                           side_effect=GhError("API rate limit exceeded")):
                    stale = client.get("/api/github/repos", headers=IDENTITY_HEADERS)
    assert stale.status_code == 200
    assert [r["nameWithOwner"] for r in stale.json()] == ["cairnea/KinDash", "acme/widget"]


# ---------------------------------------------------------------------------
# GET /api/github/branches
# ---------------------------------------------------------------------------


def test_list_github_branches(tmp_path):
    """GET /api/github/branches?repo=<o>/<n> returns default + branches."""
    with temp_env():
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            with patch(
                "ccswitch.gitrepos.list_remote_branches",
                return_value={"default": "main", "branches": ["main", "feature-x"]},
            ):
                _, client = _make_app_and_client()
                with client:
                    resp = client.get("/api/github/branches?repo=acme/widget", headers=IDENTITY_HEADERS)

    assert resp.status_code == 200
    data = resp.json()
    assert data["default"] == "main"
    assert data["branches"] == ["main", "feature-x"]


def test_list_github_branches_bad_name_400(tmp_path):
    """A malformed repo name (no slash) → 400."""
    with temp_env():
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            _, client = _make_app_and_client()
            with client:
                resp = client.get("/api/github/branches?repo=noslash", headers=IDENTITY_HEADERS)
    assert resp.status_code == 400


def test_list_github_branches_gh_error_502(tmp_path):
    from ccswitch.gitrepos import GhError
    with temp_env():
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            with patch("ccswitch.gitrepos.list_remote_branches", side_effect=GhError("404")):
                _, client = _make_app_and_client()
                with client:
                    resp = client.get("/api/github/branches?repo=acme/widget", headers=IDENTITY_HEADERS)
    assert resp.status_code == 502


# ---------------------------------------------------------------------------
# GET/DELETE /api/github/credential-alerts (issue #670)
# ---------------------------------------------------------------------------


def test_credential_alerts_lists_and_dismisses(tmp_path):
    """The banner endpoint surfaces stale-credential repos from the registry,
    and DELETE clears one."""
    from ccswitch import gitrepos
    gitrepos._stale_credentials.clear()
    gitrepos.record_fetch_result(
        "cairnea/KinDash", ok=False,
        stderr="remote: Invalid username or token. "
               "Password authentication is not supported")
    try:
        with temp_env():
            with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
                _, client = _make_app_and_client()
                with client:
                    resp = client.get("/api/github/credential-alerts", headers=IDENTITY_HEADERS)
                    assert resp.status_code == 200
                    alerts = resp.json()["alerts"]
                    assert [a["repo"] for a in alerts] == ["cairnea/KinDash"]
                    assert alerts[0]["settingsUrl"] == gitrepos.token_settings_url()

                    dresp = client.request(
                        "DELETE", "/api/github/credential-alerts",
                        params={"repo": "cairnea/KinDash"}, headers=IDENTITY_HEADERS)
                    assert dresp.status_code == 200
                    assert dresp.json()["cleared"] is True

                    # After dismissal the list is empty.
                    resp2 = client.get("/api/github/credential-alerts", headers=IDENTITY_HEADERS)
                    assert resp2.json()["alerts"] == []
    finally:
        gitrepos._stale_credentials.clear()


def test_credential_alerts_empty_when_clean(tmp_path):
    from ccswitch import gitrepos
    gitrepos._stale_credentials.clear()
    with temp_env():
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            _, client = _make_app_and_client()
            with client:
                resp = client.get("/api/github/credential-alerts", headers=IDENTITY_HEADERS)
    assert resp.status_code == 200
    assert resp.json() == {"alerts": []}


# ---------------------------------------------------------------------------
# GET /api/config
# ---------------------------------------------------------------------------


def test_get_config_returns_repo_root():
    """GET /api/config returns the configured repoRoot."""
    with temp_env():
        with patch.dict(os.environ, {"CCSWITCH_REPO_ROOT": "/home/test/GitHub", "CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            _, client = _make_app_and_client()
            with client:
                resp = client.get("/api/config", headers=IDENTITY_HEADERS)

    assert resp.status_code == 200
    data = resp.json()
    assert data["repoRoot"] == "/home/test/GitHub"


# ---------------------------------------------------------------------------
# POST /api/sessions with repo= (clone-on-select)
# ---------------------------------------------------------------------------


def test_create_session_with_repo_clones(tmp_path):
    """POST /api/sessions with repo= clones into the ccswitch namespace and runs there."""
    from ccswitch import paths
    import time as _time

    # A real local clone dir that ensure_clone will "return". It needs at least one
    # commit so that the per-session worktree (git worktree add --detach off HEAD,
    # created for every repo session now) has a commit to detach from.
    clone_dir = tmp_path / ".ccswitch" / "clones" / "acme" / "widget"
    clone_dir.mkdir(parents=True)
    subprocess.run(["git", "init", str(clone_dir), "-b", "main"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(clone_dir), "config", "user.email", "t@example.com"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(clone_dir), "config", "user.name", "Test"], check=True, capture_output=True)
    (clone_dir / "README.md").write_text("x\n")
    subprocess.run(["git", "-C", str(clone_dir), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(clone_dir), "commit", "-m", "init"], check=True, capture_output=True)

    captured: dict = {}

    def fake_ensure_clone(repo_root, name_with_owner, url):
        captured["repo_root"] = repo_root
        captured["name_with_owner"] = name_with_owner
        captured["url"] = url
        return str(clone_dir)

    with temp_env():
        store = paths.store_dir()
        store.mkdir(parents=True, exist_ok=True)
        now_ms = int(_time.time() * 1000)
        slot = store / "account1.cred.json"
        slot.write_text(f'{{"accessToken":"tok","refreshToken":"ref","expiresAt":{now_ms+9_000_000},"subscriptionType":"max"}}')
        cfg_dir = paths.account_cfg_dir("account1")
        cfg_dir.mkdir(parents=True, exist_ok=True)

        with patch("server.sessions.orchestrator.select_account", return_value=Picked("account1")), \
             patch("server.sessions.orchestrator.compose", return_value=cfg_dir), \
             patch("ccswitch.gitrepos.ensure_clone", side_effect=fake_ensure_clone), \
             patch.dict(os.environ, {"CCSWITCH_REPO_ROOT": str(tmp_path), "CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):

            from server.sessions.orchestrator import SessionOrchestrator
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

    assert resp.status_code == 201
    assert captured["name_with_owner"] == "acme/widget"
    assert captured["url"] == "https://github.com/acme/widget.git"


def test_create_session_accepts_attachment_ids(tmp_path):
    """POST /api/sessions accepts attachmentIds and defers them for first attach."""
    import base64
    import time as _time
    from ccswitch import paths

    # 1×1 PNG used for a real upload.
    PNG = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
        "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")

    with temp_env():
        store = paths.store_dir()
        store.mkdir(parents=True, exist_ok=True)
        now_ms = int(_time.time() * 1000)
        (store / "account1.cred.json").write_text(
            f'{{"accessToken":"t","refreshToken":"r","expiresAt":{now_ms+9_000_000},"subscriptionType":"max"}}')
        cfg_dir = paths.account_cfg_dir("account1")
        cfg_dir.mkdir(parents=True, exist_ok=True)

        with patch("server.sessions.orchestrator.select_account", return_value=Picked("account1")), \
             patch("server.sessions.orchestrator.compose", return_value=cfg_dir), \
             patch.dict(os.environ, {"CCSWITCH_REPO_ROOT": str(tmp_path),
                                     "CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            from server.sessions.orchestrator import SessionOrchestrator
            from server.app import create_app
            from fastapi.testclient import TestClient
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0)
            app = create_app(orchestrator=orch)
            client = TestClient(app, raise_server_exceptions=False)
            with client:
                up = client.post("/api/attachments", headers=IDENTITY_HEADERS,
                                 files={"file": ("x.png", PNG, "image/png")})
                assert up.status_code == 201
                aid = up.json()["id"]
                resp = client.post("/api/sessions", headers=IDENTITY_HEADERS, json={
                    "name": "t", "initialPrompt": "see this",
                    "attachmentIds": [aid]})
            assert resp.status_code == 201
            sid = resp.json()["id"]
            assert orch.pop_pending_initial_attachment_ids(sid) == [aid]


def test_create_session_clone_error_502(tmp_path):
    """A CloneError from gitrepos.ensure_clone → HTTP 502."""
    from ccswitch.gitrepos import CloneError
    with temp_env():
        with patch("ccswitch.gitrepos.ensure_clone", side_effect=CloneError("boom")), \
             patch.dict(os.environ, {"CCSWITCH_REPO_ROOT": str(tmp_path), "CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            from server.sessions.orchestrator import SessionOrchestrator
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
    assert resp.status_code == 502
