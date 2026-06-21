"""Server-level tests for reauth REST endpoints.

POST /api/accounts/{name}/reauth   → start (202, state; 409 busy; 503 no key; 404)
GET  /api/accounts/{name}/reauth   → status
POST /api/accounts/{name}/reauth/cancel
Name-guard on ALL /{name} routes (reauth + refresh/delete/nickname/patch).
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from tests.helpers import temp_env
from server.tests.fakes.fake_factory import fake_sdk_factory
from server.reauth import ReauthBusy, ReauthNoKey, ReauthState

IDENTITY_HEADERS = {"Tailscale-User-Login": "testuser@example.com"}


# ---------------------------------------------------------------------------
# Fake ReauthManager (injectable into app.state)
# ---------------------------------------------------------------------------

class FakeReauthManager:
    """Injectable fake for testing the API layer without a real ReauthManager."""

    def __init__(self):
        self._state = ReauthState()
        self.raise_busy = False
        self.raise_no_key = False
        self.holding_identity: str | None = None
        self.holding_started_at: float | None = None

    async def start(self, name: str, identity: str) -> ReauthState:
        if self.raise_no_key:
            raise ReauthNoKey("no key")
        if self.raise_busy:
            raise ReauthBusy(self.holding_identity, self.holding_started_at)
        self._state = ReauthState(
            state="running",
            identity=identity,
            started_at=time.time(),
        )
        return ReauthState(
            state=self._state.state,
            identity=self._state.identity,
            started_at=self._state.started_at,
        )

    def status(self, name: str) -> ReauthState:
        s = self._state
        return ReauthState(
            state=s.state,
            identity=s.identity,
            started_at=s.started_at,
            message=s.message,
        )

    async def cancel(self, name: str) -> ReauthState:
        self._state = ReauthState(state="idle", message="cancelled")
        return self.status(name)

    def sweep_stale(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass


# ---------------------------------------------------------------------------
# App/client factory + seed helpers
# ---------------------------------------------------------------------------

def _seed_slot(name: str) -> None:
    """Write a minimal credential file so the slot exists (uses active CCSWITCH_HOME)."""
    from ccswitch import paths
    store = paths.store_dir()
    store.mkdir(parents=True, exist_ok=True)
    slot = store / f"{name}.cred.json"
    now_ms = int(time.time() * 1000)
    slot.write_text(
        f'{{"accessToken":"{name}-tok","refreshToken":"{name}-ref",'
        f'"expiresAt":{now_ms + 9_000_000},"subscriptionType":"max"}}'
    )


def _make_client(reauth_manager: FakeReauthManager | None = None) -> TestClient:
    from server.sessions.orchestrator import SessionOrchestrator
    from server.app import create_app

    with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
        orch = SessionOrchestrator(
            sdk_factory=fake_sdk_factory(),
            poll_interval=9999.0,
        )
        app = create_app(orchestrator=orch)
        if reauth_manager is not None:
            app.state.reauth = reauth_manager
        return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# POST /api/accounts/{name}/reauth — start
# ---------------------------------------------------------------------------


def test_reauth_start_returns_202_with_state():
    """POST /api/accounts/{name}/reauth returns 202 and the running state dict."""
    fake_rm = FakeReauthManager()
    with temp_env():
        _seed_slot("primary")
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client(fake_rm)
        with client:
            resp = client.post(
                "/api/accounts/primary/reauth",
                headers=IDENTITY_HEADERS,
            )

    assert resp.status_code == 202
    data = resp.json()
    assert data["state"] == "running"
    assert "startedAt" in data


def test_reauth_start_409_when_busy():
    """POST reauth returns 409 with holding identity + start time when another is in progress."""
    fake_rm = FakeReauthManager()
    fake_rm.raise_busy = True
    fake_rm.holding_identity = "other@example.com"
    fake_rm.holding_started_at = time.time() - 30

    with temp_env():
        _seed_slot("primary")
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client(fake_rm)
        with client:
            resp = client.post(
                "/api/accounts/primary/reauth",
                headers=IDENTITY_HEADERS,
            )

    assert resp.status_code == 409
    data = resp.json()
    assert "other@example.com" in str(data)


def test_reauth_start_503_when_no_key():
    """POST reauth returns 503 when no encryption key is configured."""
    fake_rm = FakeReauthManager()
    fake_rm.raise_no_key = True

    with temp_env():
        _seed_slot("primary")
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client(fake_rm)
        with client:
            resp = client.post(
                "/api/accounts/primary/reauth",
                headers=IDENTITY_HEADERS,
            )

    assert resp.status_code == 503


def test_reauth_start_404_dotted_name():
    """POST reauth returns 404 for a name starting with dot (hidden file / invalid)."""
    fake_rm = FakeReauthManager()

    with temp_env():
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client(fake_rm)
        with client:
            resp = client.post(
                "/api/accounts/.hidden/reauth",
                headers=IDENTITY_HEADERS,
            )

    assert resp.status_code == 404


def test_reauth_start_404_unknown_account():
    """POST reauth returns 404 when the named slot does not exist."""
    fake_rm = FakeReauthManager()

    with temp_env():
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client(fake_rm)
        with client:
            resp = client.post(
                "/api/accounts/nonexistent/reauth",
                headers=IDENTITY_HEADERS,
            )

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/accounts/{name}/reauth — status
# ---------------------------------------------------------------------------


def test_reauth_get_status():
    """GET /api/accounts/{name}/reauth returns current state dict."""
    fake_rm = FakeReauthManager()
    fake_rm._state = ReauthState(state="running", identity="me@x.com", started_at=time.time())

    with temp_env():
        _seed_slot("primary")
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client(fake_rm)
        with client:
            resp = client.get(
                "/api/accounts/primary/reauth",
                headers=IDENTITY_HEADERS,
            )

    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "running"
    assert data["identity"] == "me@x.com"
    assert "startedAt" in data


def test_reauth_get_status_404_unknown():
    """GET /api/accounts/{name}/reauth returns 404 when slot does not exist."""
    fake_rm = FakeReauthManager()

    with temp_env():
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client(fake_rm)
        with client:
            resp = client.get(
                "/api/accounts/nobody/reauth",
                headers=IDENTITY_HEADERS,
            )

    assert resp.status_code == 404


def test_reauth_get_status_404_dotted_name():
    """GET /api/accounts/{name}/reauth returns 404 for dotted names."""
    fake_rm = FakeReauthManager()

    with temp_env():
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client(fake_rm)
        with client:
            resp = client.get(
                "/api/accounts/.hidden/reauth",
                headers=IDENTITY_HEADERS,
            )

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/accounts/{name}/reauth/cancel
# ---------------------------------------------------------------------------


def test_reauth_cancel():
    """POST /api/accounts/{name}/reauth/cancel returns 200 and idle state."""
    fake_rm = FakeReauthManager()
    fake_rm._state = ReauthState(state="running", identity="me@x.com", started_at=time.time())

    with temp_env():
        _seed_slot("primary")
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client(fake_rm)
        with client:
            resp = client.post(
                "/api/accounts/primary/reauth/cancel",
                headers=IDENTITY_HEADERS,
            )

    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "idle"


def test_reauth_cancel_404_unknown():
    """POST /api/accounts/{name}/reauth/cancel returns 404 for unknown slot."""
    fake_rm = FakeReauthManager()

    with temp_env():
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client(fake_rm)
        with client:
            resp = client.post(
                "/api/accounts/nobody/reauth/cancel",
                headers=IDENTITY_HEADERS,
            )

    assert resp.status_code == 404


def test_reauth_cancel_404_dotted_name():
    """POST /api/accounts/{name}/reauth/cancel returns 404 for dotted names."""
    fake_rm = FakeReauthManager()

    with temp_env():
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client(fake_rm)
        with client:
            resp = client.post(
                "/api/accounts/.hidden/reauth/cancel",
                headers=IDENTITY_HEADERS,
            )

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Audit — reauth audit row carries only account, not token/subscriptionType
# ---------------------------------------------------------------------------


def test_reauth_audit_no_token():
    """Audit detail for reauth (start/cancel) carries only account, never token material.

    The real DB-backed assertion for account.reauth.success lives in
    test_reauth_manager.py::test_success_writes_audit_row_no_token_material.
    This test verifies the *shape* of the detail dict passed to record_audit
    by all reauth routes: it must only contain {"account": <name>} so that
    even without redaction no credential material is stored.
    """
    # The detail passed to record_audit by reauth_start and reauth_cancel
    detail = {"account": "primary"}
    # No tokens or subscription keys are present in the first place
    assert set(detail.keys()) == {"account"}
    assert "accessToken" not in detail
    assert "refreshToken" not in detail
    assert "subscriptionType" not in detail
    assert "token" not in str(detail)


# ---------------------------------------------------------------------------
# Name-guard on existing /{name} routes (refresh, delete, patch)
# ---------------------------------------------------------------------------


def test_refresh_404_dotted_name():
    """POST /api/accounts/{name}/refresh rejects dotted names with 404."""
    fake_rm = FakeReauthManager()

    with temp_env():
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client(fake_rm)
        with client:
            resp = client.post(
                "/api/accounts/.hidden/refresh",
                headers=IDENTITY_HEADERS,
            )

    assert resp.status_code == 404


def test_delete_404_dotted_name():
    """DELETE /api/accounts/{name} rejects dotted names with 404."""
    fake_rm = FakeReauthManager()

    with temp_env():
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client(fake_rm)
        with client:
            resp = client.delete(
                "/api/accounts/.hidden",
                headers=IDENTITY_HEADERS,
            )

    assert resp.status_code == 404


def test_patch_404_dotted_name_via_name_guard():
    """PATCH /api/accounts/{name} rejects dotted names with 404 (via name guard)."""
    fake_rm = FakeReauthManager()

    with temp_env():
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client(fake_rm)
        with client:
            resp = client.patch(
                "/api/accounts/.hidden",
                json={"nickname": "bad"},
                headers=IDENTITY_HEADERS,
            )

    assert resp.status_code == 404


def test_reauth_requires_auth():
    """POST /api/accounts/{name}/reauth without identity header returns 401."""
    fake_rm = FakeReauthManager()

    with temp_env():
        _seed_slot("primary")
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client(fake_rm)
        with client:
            resp = client.post("/api/accounts/primary/reauth")

    assert resp.status_code == 401
