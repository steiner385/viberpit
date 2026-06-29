"""Server-level tests for the add-account REST endpoints.

POST /api/accounts/add         → start an add (202 state; 409 exists/busy; 503 no key; 400 bad name)
GET  /api/accounts/add         → status of the in-flight interactive login
POST /api/accounts/add/cancel  → cancel a running add

Add-account reuses the global ReauthManager (one interactive browser login at a
time), so these routes drive the same state machine as reauth — the difference
is the precondition: the slot must NOT already exist.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from tests.helpers import temp_env
from server.reauth import ReauthState
# Reuse the reauth API test harness rather than duplicating it.
from server.tests.test_reauth_api import (
    FakeReauthManager,
    _make_client,
    _seed_slot,
    IDENTITY_HEADERS,
)


# ---------------------------------------------------------------------------
# POST /api/accounts/add — start
# ---------------------------------------------------------------------------


def test_add_start_returns_202_with_state():
    """POST /api/accounts/add returns 202 and the running state for a fresh name."""
    fake_rm = FakeReauthManager()
    with temp_env():
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client(fake_rm)
        with client:
            resp = client.post(
                "/api/accounts/add",
                json={"name": "newacct"},
                headers=IDENTITY_HEADERS,
            )

    assert resp.status_code == 202
    data = resp.json()
    assert data["state"] == "running"
    assert "startedAt" in data


def test_add_start_409_when_slot_exists():
    """POST /api/accounts/add returns 409 when an account with that name already exists."""
    fake_rm = FakeReauthManager()
    with temp_env():
        _seed_slot("existing")
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client(fake_rm)
        with client:
            resp = client.post(
                "/api/accounts/add",
                json={"name": "existing"},
                headers=IDENTITY_HEADERS,
            )

    assert resp.status_code == 409
    assert "exist" in str(resp.json()).lower()


def test_add_start_400_invalid_name():
    """POST /api/accounts/add returns 400 for a malformed / path-traversal name."""
    fake_rm = FakeReauthManager()
    with temp_env():
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client(fake_rm)
        with client:
            resp = client.post(
                "/api/accounts/add",
                json={"name": "../escape"},
                headers=IDENTITY_HEADERS,
            )

    assert resp.status_code == 400


def test_add_start_400_invalid_name_has_human_message():
    """A 400 invalid_name carries a human-readable `message` so the UI can show
    specific guidance instead of a generic 'Something went wrong' toast."""
    fake_rm = FakeReauthManager()
    with temp_env():
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client(fake_rm)
        with client:
            resp = client.post(
                "/api/accounts/add",
                json={"name": "tony@steinonline.net"},
                headers=IDENTITY_HEADERS,
            )

    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["error"] == "invalid_name"
    assert isinstance(detail.get("message"), str) and detail["message"].strip()


def test_add_start_400_dotted_name():
    """POST /api/accounts/add returns 400 for a hidden-file (dot-prefixed) name."""
    fake_rm = FakeReauthManager()
    with temp_env():
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client(fake_rm)
        with client:
            resp = client.post(
                "/api/accounts/add",
                json={"name": ".hidden"},
                headers=IDENTITY_HEADERS,
            )

    assert resp.status_code == 400


def test_add_start_409_when_busy():
    """POST /api/accounts/add returns 409 when another interactive login is in progress."""
    fake_rm = FakeReauthManager()
    fake_rm.raise_busy = True
    fake_rm.holding_identity = "other@example.com"
    fake_rm.holding_started_at = time.time() - 30

    with temp_env():
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client(fake_rm)
        with client:
            resp = client.post(
                "/api/accounts/add",
                json={"name": "newacct"},
                headers=IDENTITY_HEADERS,
            )

    assert resp.status_code == 409
    assert "other@example.com" in str(resp.json())


def test_add_start_503_when_no_key():
    """POST /api/accounts/add returns 503 when no encryption key is configured."""
    fake_rm = FakeReauthManager()
    fake_rm.raise_no_key = True

    with temp_env():
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client(fake_rm)
        with client:
            resp = client.post(
                "/api/accounts/add",
                json={"name": "newacct"},
                headers=IDENTITY_HEADERS,
            )

    assert resp.status_code == 503


def test_add_start_requires_auth():
    """POST /api/accounts/add without an identity header returns 401."""
    fake_rm = FakeReauthManager()
    with temp_env():
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client(fake_rm)
        with client:
            resp = client.post("/api/accounts/add", json={"name": "newacct"})

    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /api/accounts/add — status
# ---------------------------------------------------------------------------


def test_add_status_returns_global_state():
    """GET /api/accounts/add returns the current interactive-login state.

    No slot exists yet during an add, so this must NOT 404 on a missing slot.
    """
    fake_rm = FakeReauthManager()
    fake_rm._state = ReauthState(state="running", identity="me@x.com", started_at=time.time())

    with temp_env():
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client(fake_rm)
        with client:
            resp = client.get("/api/accounts/add", headers=IDENTITY_HEADERS)

    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "running"
    assert data["identity"] == "me@x.com"


def test_add_status_requires_auth():
    """GET /api/accounts/add without an identity header returns 401."""
    fake_rm = FakeReauthManager()
    with temp_env():
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client(fake_rm)
        with client:
            resp = client.get("/api/accounts/add")

    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# POST /api/accounts/add/cancel
# ---------------------------------------------------------------------------


def test_add_cancel_returns_idle():
    """POST /api/accounts/add/cancel cancels the in-flight login and returns idle."""
    fake_rm = FakeReauthManager()
    fake_rm._state = ReauthState(state="running", identity="me@x.com", started_at=time.time())

    with temp_env():
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client(fake_rm)
        with client:
            resp = client.post("/api/accounts/add/cancel", headers=IDENTITY_HEADERS)

    assert resp.status_code == 200
    assert resp.json()["state"] == "idle"
