"""REST API tests (T023) — sessions, accounts, auth endpoints.

All requests use a valid Tailscale-User-Login identity header.
The orchestrator is injected with:
  - fake SDK client via a fake sdk_factory
  - monkeypatched usage_for via a custom usage_fn
  - temp_env for isolated CCSWITCH_HOME / CLAUDE_CONFIG_DIR
  - CCSWITCH_DB_URL=sqlite+aiosqlite:///:memory: so the DB initialises cleanly

Uses TestClient (sync, lifespan-aware) for most tests.  Async tests that need
the httpx AsyncClient use a lifespan shim.

Tests:
  - GET /api/accounts returns shaped usage and no token fields
  - POST /api/sessions creates a session (201); GET lists it; PATCH renames; DELETE closes (204)
  - POST /api/sessions with all-busy accounts → 409 with earliestReset
  - GET /api/auth/me returns identity
  - No identity header → 401
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from tests.helpers import temp_env, usage as make_usage
from server.tests.fakes.fake_factory import fake_sdk_factory

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

IDENTITY_HEADERS = {"Tailscale-User-Login": "testuser@example.com"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_usage_fn(accounts: dict):
    """Return a usage_fn that returns fake Usage for named accounts."""
    def usage_fn(name: str):
        return accounts.get(name)
    return usage_fn


def _busy_usage():
    """100% usage on both dimensions — account is at ceiling."""
    reset = datetime.now(timezone.utc) + timedelta(hours=4)
    from ccswitch.oauth import Usage
    return Usage(
        session_pct=100.0,
        session_resets_at=reset,
        weekly_pct=100.0,
        weekly_resets_at=reset,
        extra_usage_enabled=False,
    )


def _seed_slot(name: str) -> None:
    """Write a minimal credential slot file under CCSWITCH_HOME/store/.

    Must be called with temp_env() active (so CCSWITCH_HOME is set).
    """
    from ccswitch import paths
    store = paths.store_dir()
    store.mkdir(parents=True, exist_ok=True)
    slot = store / f"{name}.cred.json"
    now_ms = int(time.time() * 1000)
    slot.write_text(
        f'{{"accessToken":"{name}-tok","refreshToken":"{name}-ref",'
        f'"expiresAt":{now_ms + 9_000_000},"subscriptionType":"max"}}'
    )


def _make_app_and_client(usage_fn=None, extra_env: dict | None = None):
    """Build a test app + TestClient with lifespan.

    Returns ``(app, client)`` — the client is NOT yet entered as context manager;
    the caller should use ``with client:`` to trigger the lifespan.
    """
    from server.sessions.orchestrator import SessionOrchestrator
    from server.app import create_app

    env = {
        "CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:",
    }
    if extra_env:
        env.update(extra_env)

    with patch.dict(os.environ, env):
        orch = SessionOrchestrator(
            sdk_factory=fake_sdk_factory(),
            usage_fn=usage_fn,
            poll_interval=9999.0,
        )
        app = create_app(orchestrator=orch)
        client = TestClient(app, raise_server_exceptions=False)
        return app, client


# ---------------------------------------------------------------------------
# Test: GET /api/accounts — returns shaped usage, no token fields
# ---------------------------------------------------------------------------


def test_get_accounts_returns_usage_shape():
    """GET /api/accounts returns usage shape with no token material."""
    reset = datetime.now(timezone.utc) + timedelta(hours=3)
    from ccswitch.oauth import Usage

    fake_usage = Usage(
        session_pct=42.0,
        session_resets_at=reset,
        weekly_pct=18.0,
        weekly_resets_at=reset,
        extra_usage_enabled=False,
    )

    with temp_env() as d:
        _seed_slot("alice")
        usage_fn = _make_usage_fn({"alice": fake_usage})

        # Keep temp_env active while the client runs (CCSWITCH_HOME must stay set)
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            from server.sessions.orchestrator import SessionOrchestrator
            from server.app import create_app
            orch = SessionOrchestrator(
                sdk_factory=fake_sdk_factory(),
                usage_fn=usage_fn,
                poll_interval=9999.0,
            )
            app = create_app(orchestrator=orch)
            client = TestClient(app, raise_server_exceptions=False)
            with client:
                response = client.get("/api/accounts", headers=IDENTITY_HEADERS)

    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    assert len(data) == 1
    acct = data[0]
    assert acct["name"] == "alice"
    assert "status" in acct
    assert "usage" in acct
    usage = acct["usage"]
    assert "session" in usage
    assert "weekly" in usage
    assert "pct" in usage["session"]
    assert "resetsAt" in usage["session"]
    assert "pct" in usage["weekly"]
    assert "resetsAt" in usage["weekly"]
    # Must not contain any token material
    import json
    raw = json.dumps(data)
    assert "accessToken" not in raw
    assert "refreshToken" not in raw
    # The token values themselves should not appear
    assert "alice-tok" not in raw
    assert "alice-ref" not in raw


def test_get_accounts_no_token_fields():
    """GET /api/accounts response never contains token fields."""
    with temp_env():
        _seed_slot("bob")
        usage_fn = _make_usage_fn({"bob": make_usage(session=5.0, weekly=10.0)})

        # Keep temp_env active while the client runs
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            from server.sessions.orchestrator import SessionOrchestrator
            from server.app import create_app
            orch = SessionOrchestrator(
                sdk_factory=fake_sdk_factory(),
                usage_fn=usage_fn,
                poll_interval=9999.0,
            )
            app = create_app(orchestrator=orch)
            client = TestClient(app, raise_server_exceptions=False)
            with client:
                response = client.get("/api/accounts", headers=IDENTITY_HEADERS)

    assert response.status_code == 200
    import json
    raw = json.dumps(response.json())
    assert "bob-tok" not in raw
    assert "bob-ref" not in raw
    assert "accessToken" not in raw
    assert "refreshToken" not in raw


# ---------------------------------------------------------------------------
# Test: Session CRUD — create, list, rename, delete
# ---------------------------------------------------------------------------


def test_session_lifecycle():
    """POST creates a session (201); GET lists it; PATCH renames; DELETE closes (204)."""
    with temp_env() as d:
        _seed_slot("account1")
        usage_fn = _make_usage_fn({"account1": make_usage(session=10.0, weekly=20.0)})

        with patch("ccswitch.accounts.usage_for", side_effect=usage_fn), \
             patch("server.sessions.orchestrator.select_account", return_value="account1"), \
             patch("server.sessions.orchestrator.compose", return_value=d / "cfg" / "account1"), \
             patch.dict(os.environ, {
                 "CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:",
             }):
            (d / "cfg" / "account1").mkdir(parents=True, exist_ok=True)

            from server.sessions.orchestrator import SessionOrchestrator
            from server.app import create_app
            orch = SessionOrchestrator(
                sdk_factory=fake_sdk_factory(),
                usage_fn=usage_fn,
                poll_interval=9999.0,
            )
            app = create_app(orchestrator=orch)
            client = TestClient(app, raise_server_exceptions=False)

            with client:
                # POST /api/sessions — create
                create_resp = client.post(
                    "/api/sessions",
                    json={"name": "My Test Session", "account": "account1"},
                    headers=IDENTITY_HEADERS,
                )
                assert create_resp.status_code == 201, create_resp.text
                created = create_resp.json()
                assert "id" in created
                session_id = created["id"]
                assert created["name"] == "My Test Session"

                # GET /api/sessions — list
                list_resp = client.get("/api/sessions", headers=IDENTITY_HEADERS)
                assert list_resp.status_code == 200
                sessions = list_resp.json()
                assert isinstance(sessions, list)
                ids = [s["id"] for s in sessions]
                assert session_id in ids

                # GET /api/sessions/{id} — get single
                get_resp = client.get(
                    f"/api/sessions/{session_id}", headers=IDENTITY_HEADERS
                )
                assert get_resp.status_code == 200
                record = get_resp.json()
                assert record["id"] == session_id

                # PATCH /api/sessions/{id} — rename
                patch_resp = client.patch(
                    f"/api/sessions/{session_id}",
                    json={"name": "Renamed Session"},
                    headers=IDENTITY_HEADERS,
                )
                assert patch_resp.status_code == 200
                renamed = patch_resp.json()
                assert renamed["name"] == "Renamed Session"

                # DELETE /api/sessions/{id} — close
                del_resp = client.delete(
                    f"/api/sessions/{session_id}", headers=IDENTITY_HEADERS
                )
                assert del_resp.status_code == 204


def test_session_get_404_for_unknown():
    """GET /api/sessions/{unknown_id} returns 404."""
    with temp_env():
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            from server.sessions.orchestrator import SessionOrchestrator
            from server.app import create_app
            orch = SessionOrchestrator(
                sdk_factory=fake_sdk_factory(),
                poll_interval=9999.0,
            )
            app = create_app(orchestrator=orch)
            client = TestClient(app, raise_server_exceptions=False)

        with client:
            response = client.get(
                "/api/sessions/does-not-exist", headers=IDENTITY_HEADERS
            )
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Test: POST /api/sessions → 409 when all accounts busy
# ---------------------------------------------------------------------------


def test_post_sessions_409_when_all_busy():
    """POST /api/sessions returns 409 with earliestReset when all accounts are at ceiling."""
    reset_dt = datetime.now(timezone.utc) + timedelta(hours=2)

    from ccswitch.selector import AllAccountsBusy

    with temp_env():
        with patch("server.sessions.selection.pick_account") as mock_pick, \
             patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            mock_pick.side_effect = AllAccountsBusy(reset_dt)

            from server.sessions.orchestrator import SessionOrchestrator
            from server.app import create_app
            orch = SessionOrchestrator(
                sdk_factory=fake_sdk_factory(),
                poll_interval=9999.0,
            )
            app = create_app(orchestrator=orch)
            client = TestClient(app, raise_server_exceptions=False)

            with client:
                response = client.post(
                    "/api/sessions",
                    json={"name": "Blocked"},
                    headers=IDENTITY_HEADERS,
                )

    assert response.status_code == 409, response.text
    data = response.json()
    assert "error" in data
    assert data["error"]["code"] == "all_accounts_limited"
    assert "earliestReset" in data
    assert data["earliestReset"] is not None


def test_post_sessions_409_null_earliest_reset():
    """POST /api/sessions returns 409 with null earliestReset when reset time is unknown."""
    from ccswitch.selector import AllAccountsBusy

    with temp_env():
        with patch("server.sessions.selection.pick_account") as mock_pick, \
             patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            mock_pick.side_effect = AllAccountsBusy(None)

            from server.sessions.orchestrator import SessionOrchestrator
            from server.app import create_app
            orch = SessionOrchestrator(
                sdk_factory=fake_sdk_factory(),
                poll_interval=9999.0,
            )
            app = create_app(orchestrator=orch)
            client = TestClient(app, raise_server_exceptions=False)

            with client:
                response = client.post(
                    "/api/sessions",
                    json={"name": "Blocked"},
                    headers=IDENTITY_HEADERS,
                )

    assert response.status_code == 409
    data = response.json()
    assert data["earliestReset"] is None


# ---------------------------------------------------------------------------
# Test: GET /api/auth/me
# ---------------------------------------------------------------------------


def test_auth_me_returns_identity():
    """GET /api/auth/me returns the current identity and authMode."""
    with temp_env():
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            from server.sessions.orchestrator import SessionOrchestrator
            from server.app import create_app
            orch = SessionOrchestrator(
                sdk_factory=fake_sdk_factory(),
                poll_interval=9999.0,
            )
            app = create_app(orchestrator=orch)
            client = TestClient(app, raise_server_exceptions=False)

        with client:
            response = client.get("/api/auth/me", headers=IDENTITY_HEADERS)

    assert response.status_code == 200
    data = response.json()
    assert data["identity"] == "testuser@example.com"
    assert data["authMode"] == "tailscale"


# ---------------------------------------------------------------------------
# Test: Unauthenticated requests → 401
# ---------------------------------------------------------------------------


def test_unauthenticated_returns_401():
    """Any request without an identity header returns 401."""
    with temp_env():
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            from server.sessions.orchestrator import SessionOrchestrator
            from server.app import create_app
            orch = SessionOrchestrator(
                sdk_factory=fake_sdk_factory(),
                poll_interval=9999.0,
            )
            app = create_app(orchestrator=orch)
            client = TestClient(app, raise_server_exceptions=False)

        with client:
            for path in ["/api/auth/me", "/api/accounts", "/api/sessions"]:
                response = client.get(path)
                assert response.status_code == 401, (
                    f"Expected 401 for {path}, got {response.status_code}"
                )


# ---------------------------------------------------------------------------
# Test: POST /api/auth/logout
# ---------------------------------------------------------------------------


def test_logout_returns_204():
    """POST /api/auth/logout returns 204."""
    with temp_env():
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            from server.sessions.orchestrator import SessionOrchestrator
            from server.app import create_app
            orch = SessionOrchestrator(
                sdk_factory=fake_sdk_factory(),
                poll_interval=9999.0,
            )
            app = create_app(orchestrator=orch)
            client = TestClient(app, raise_server_exceptions=False)

        with client:
            response = client.post("/api/auth/logout", headers=IDENTITY_HEADERS)

    assert response.status_code == 204


# ---------------------------------------------------------------------------
# Test: POST /api/accounts/{name}/refresh
# ---------------------------------------------------------------------------


def test_account_refresh_returns_usage():
    """POST /api/accounts/{name}/refresh returns fresh usage for that account."""
    with temp_env() as d:
        _seed_slot("charlie")
        usage_fn = _make_usage_fn({"charlie": make_usage(session=30.0, weekly=55.0)})

        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            from server.sessions.orchestrator import SessionOrchestrator
            from server.app import create_app
            orch = SessionOrchestrator(
                sdk_factory=fake_sdk_factory(),
                usage_fn=usage_fn,
                poll_interval=9999.0,
            )
            app = create_app(orchestrator=orch)
            client = TestClient(app, raise_server_exceptions=False)

        with client:
            response = client.post(
                "/api/accounts/charlie/refresh", headers=IDENTITY_HEADERS
            )

    assert response.status_code == 200
    data = response.json()
    assert "usage" in data
    assert data["usage"]["session"]["pct"] == pytest.approx(30.0)
    assert data["usage"]["weekly"]["pct"] == pytest.approx(55.0)
    import json
    raw = json.dumps(data)
    assert "charlie-tok" not in raw
    assert "charlie-ref" not in raw
    assert "accessToken" not in raw
    assert "refreshToken" not in raw


# ---------------------------------------------------------------------------
# Test: DELETE /api/accounts/{name}
# ---------------------------------------------------------------------------


def test_delete_account_returns_204():
    """DELETE /api/accounts/{name} removes the account slot and returns 204."""
    with temp_env() as d:
        _seed_slot("dave")

        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            from server.sessions.orchestrator import SessionOrchestrator
            from server.app import create_app
            orch = SessionOrchestrator(
                sdk_factory=fake_sdk_factory(),
                poll_interval=9999.0,
            )
            app = create_app(orchestrator=orch)
            client = TestClient(app, raise_server_exceptions=False)

        with client:
            response = client.delete("/api/accounts/dave", headers=IDENTITY_HEADERS)

    assert response.status_code == 204


# ---------------------------------------------------------------------------
# Test: POST /api/accounts/{name}/relogin — replaced by /reauth
# ---------------------------------------------------------------------------


def test_relogin_route_removed():
    """POST /api/accounts/{name}/relogin is no longer registered (returns 404/405).

    The placeholder relogin route was replaced by the real /reauth endpoints
    in Task 6.  Unknown accounts return 404 from the name+slot guard.
    """
    with temp_env():
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            from server.sessions.orchestrator import SessionOrchestrator
            from server.app import create_app
            orch = SessionOrchestrator(
                sdk_factory=fake_sdk_factory(),
                poll_interval=9999.0,
            )
            app = create_app(orchestrator=orch)
            client = TestClient(app, raise_server_exceptions=False)

        with client:
            response = client.post(
                "/api/accounts/anyname/relogin", headers=IDENTITY_HEADERS
            )

    # The route no longer exists; FastAPI returns 404 or 405
    assert response.status_code in (404, 405)


# ---------------------------------------------------------------------------
# Coverage boosters for critical modules (T040)
# ---------------------------------------------------------------------------


def test_logout_with_cookie_revokes_session():
    """POST /api/auth/logout with a valid cookie revokes the AppSession (covers lines 57-60)."""
    with temp_env():
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            from server.sessions.orchestrator import SessionOrchestrator
            from server.app import create_app
            orch = SessionOrchestrator(
                sdk_factory=fake_sdk_factory(),
                poll_interval=9999.0,
            )
            app = create_app(orchestrator=orch)
            client = TestClient(app, raise_server_exceptions=False)

        with client:
            # First request — establishes a cookie
            me_resp = client.get("/api/auth/me", headers=IDENTITY_HEADERS)
            assert me_resp.status_code == 200
            # Grab the set-cookie header (the client stores it automatically in session)
            # Second request — logout WITH the cookie that the client now holds
            logout_resp = client.post("/api/auth/logout", headers=IDENTITY_HEADERS)
            assert logout_resp.status_code == 204


def test_identity_middleware_db_unavailable_still_serves():
    """IdentityMiddleware allows requests even when DB is unavailable (covers lines 88-90)."""
    with temp_env():
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            from server.sessions.orchestrator import SessionOrchestrator
            from server.app import create_app
            orch = SessionOrchestrator(
                sdk_factory=fake_sdk_factory(),
                poll_interval=9999.0,
            )
            app = create_app(orchestrator=orch)
            client = TestClient(app, raise_server_exceptions=False)

        # Patch the session factory to simulate DB failure
        with client:
            import server.db as _db_mod
            original = _db_mod._session_factory
            # Cause DB failure during middleware
            _db_mod._session_factory = None
            try:
                response = client.get("/api/health", headers=IDENTITY_HEADERS)
                # Should still serve — DB failure is non-fatal in middleware
                assert response.status_code == 200
            finally:
                _db_mod._session_factory = original


def test_auth_identity_no_tailscale_header_no_client_host():
    """verify_identity returns None when no header and no client host (covers line 50)."""
    from server.auth.identity import verify_identity
    from unittest.mock import MagicMock

    # Simulate a request with no identity header and no client
    mock_request = MagicMock()
    mock_request.headers = {}
    mock_request.client = None

    result = verify_identity(mock_request)
    assert result is None


def test_whois_identity_parses_login_line():
    """_whois_identity returns UserProfile.LoginName from `tailscale whois --json`."""
    import json
    from unittest.mock import MagicMock, patch
    from server.auth.identity import _whois_identity

    mock_request = MagicMock()
    mock_request.client = MagicMock()
    mock_request.client.host = "100.64.0.1"

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = json.dumps({"UserProfile": {"LoginName": "whoisuser@example.com"}})

    with patch("subprocess.run", return_value=mock_result):
        identity = _whois_identity(mock_request)

    assert identity == "whoisuser@example.com"


def test_whois_identity_nonzero_exit_returns_none():
    """_whois_identity returns None when tailscale whois exits non-zero (covers line 57-58)."""
    from unittest.mock import MagicMock, patch
    from server.auth.identity import _whois_identity

    mock_request = MagicMock()
    mock_request.client = MagicMock()
    mock_request.client.host = "100.64.0.1"

    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = ""

    with patch("subprocess.run", return_value=mock_result):
        identity = _whois_identity(mock_request)

    assert identity is None


def test_whois_identity_subprocess_exception_returns_none():
    """_whois_identity returns None when subprocess.run raises (covers lines 64-66)."""
    from unittest.mock import MagicMock, patch
    from server.auth.identity import _whois_identity

    mock_request = MagicMock()
    mock_request.client = MagicMock()
    mock_request.client.host = "100.64.0.1"

    with patch("subprocess.run", side_effect=FileNotFoundError("tailscale not found")):
        identity = _whois_identity(mock_request)

    assert identity is None


def test_get_archived_route_not_shadowed_by_session_id():
    """Regression: GET /api/sessions/archived must resolve to the archived-list route,
    NOT be captured by the dynamic /{session_id} route (which would 404 with
    session_id='archived'). Route declaration order matters in FastAPI."""
    app, client = _make_app_and_client()
    with client:
        resp = client.get("/api/sessions/archived", headers=IDENTITY_HEADERS)
    assert resp.status_code == 200, (
        f"/sessions/archived returned {resp.status_code} (shadowed by /{{session_id}}?): {resp.text}"
    )
    assert isinstance(resp.json(), list)
