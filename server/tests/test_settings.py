"""Tests for GET/PUT /api/settings (AppSettings singleton row)."""
from __future__ import annotations

import os
import re
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from tests.helpers import temp_env

IDENTITY_HEADERS = {"Tailscale-User-Login": "testuser@example.com"}


@contextmanager
def _client():
    """A live-app TestClient over an isolated file-backed SQLite DB.

    A temp *file* DB (not ``:memory:``) is deliberate: with the app's live
    orchestrator, an in-memory DB forces every consumer onto one shared
    connection, so the orchestrator's startup poll races the settings
    PUT-then-GET on that connection (flaky ``interactive`` vs ``auto``). A file
    DB gives each connection its own handle to one real database, and the WAL
    pragma the engine sets makes a committed write visible to the next reader.
    """
    from server.sessions.orchestrator import SessionOrchestrator
    from server.app import create_app
    from fastapi.testclient import TestClient

    with temp_env(), tempfile.TemporaryDirectory() as d:
        db_url = f"sqlite+aiosqlite:///{Path(d) / 'settings.db'}"
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": db_url}):
            orch = SessionOrchestrator(poll_interval=9999.0)
            app = create_app(orchestrator=orch)
            client = TestClient(app, raise_server_exceptions=False)
            with client:
                yield client


# ---------------------------------------------------------------------------
# GET /api/settings
# ---------------------------------------------------------------------------


def test_get_settings_returns_defaults():
    """GET /api/settings returns the singleton row with defaults on first call."""
    with _client() as client:
        resp = client.get("/api/settings", headers=IDENTITY_HEADERS)

    assert resp.status_code == 200
    data = resp.json()
    assert data["defaultPermissionMode"] == "interactive"
    assert data["thinkingCollapsed"] is True
    assert data["repoRoot"] is None


# ---------------------------------------------------------------------------
# PUT /api/settings round-trip
# ---------------------------------------------------------------------------


def test_put_settings_round_trips():
    """PUT {defaultPermissionMode: 'auto'} then GET → 'auto'."""
    with _client() as client:
        put_resp = client.put(
            "/api/settings",
            json={"defaultPermissionMode": "auto"},
            headers=IDENTITY_HEADERS,
        )
        assert put_resp.status_code == 200
        assert put_resp.json()["defaultPermissionMode"] == "auto"

        get_resp = client.get("/api/settings", headers=IDENTITY_HEADERS)
        assert get_resp.status_code == 200
        assert get_resp.json()["defaultPermissionMode"] == "auto"


# ---------------------------------------------------------------------------
# PUT /api/settings rejects invalid mode
# ---------------------------------------------------------------------------


def test_put_rejects_invalid_mode():
    """PUT {defaultPermissionMode: 'bogus'} → 422, stored value stays 'interactive'."""
    with _client() as client:
        put_resp = client.put(
            "/api/settings",
            json={"defaultPermissionMode": "bogus"},
            headers=IDENTITY_HEADERS,
        )
        assert put_resp.status_code == 422

        get_resp = client.get("/api/settings", headers=IDENTITY_HEADERS)
        assert get_resp.status_code == 200
        assert get_resp.json()["defaultPermissionMode"] == "interactive"


# ---------------------------------------------------------------------------
# defaultToolDetail
# ---------------------------------------------------------------------------


def test_get_settings_default_tool_detail_is_auto():
    """GET /api/settings returns defaultToolDetail 'auto' by default."""
    with _client() as client:
        resp = client.get("/api/settings", headers=IDENTITY_HEADERS)

    assert resp.status_code == 200
    assert resp.json()["defaultToolDetail"] == "auto"


def test_put_default_tool_detail_round_trips():
    """PUT {defaultToolDetail: 'all-closed'} then GET → 'all-closed'."""
    with _client() as client:
        put_resp = client.put(
            "/api/settings",
            json={"defaultToolDetail": "all-closed"},
            headers=IDENTITY_HEADERS,
        )
        assert put_resp.status_code == 200
        assert put_resp.json()["defaultToolDetail"] == "all-closed"

        get_resp = client.get("/api/settings", headers=IDENTITY_HEADERS)
        assert get_resp.json()["defaultToolDetail"] == "all-closed"


def test_put_rejects_invalid_tool_detail():
    """PUT {defaultToolDetail: 'bogus'} → 422, stored value stays 'auto'."""
    with _client() as client:
        put_resp = client.put(
            "/api/settings",
            json={"defaultToolDetail": "bogus"},
            headers=IDENTITY_HEADERS,
        )
        assert put_resp.status_code == 422

        get_resp = client.get("/api/settings", headers=IDENTITY_HEADERS)
        assert get_resp.status_code == 200
        assert get_resp.json()["defaultToolDetail"] == "auto"

# ---------------------------------------------------------------------------
# maxArchivedSessions
# ---------------------------------------------------------------------------


def test_get_settings_max_archived_defaults_none():
    """GET /api/settings returns maxArchivedSessions null by default (unlimited)."""
    with _client() as client:
        resp = client.get("/api/settings", headers=IDENTITY_HEADERS)

    assert resp.status_code == 200
    assert resp.json()["maxArchivedSessions"] is None


def test_put_max_archived_round_trips():
    """PUT {maxArchivedSessions: 50} then GET → 50."""
    with _client() as client:
        put_resp = client.put(
            "/api/settings",
            json={"maxArchivedSessions": 50},
            headers=IDENTITY_HEADERS,
        )
        assert put_resp.status_code == 200
        assert put_resp.json()["maxArchivedSessions"] == 50

        get_resp = client.get("/api/settings", headers=IDENTITY_HEADERS)
        assert get_resp.json()["maxArchivedSessions"] == 50


def test_put_max_archived_zero_clears_to_unlimited():
    """0 (and any <=0) clears the cap back to null (unlimited)."""
    with _client() as client:
        client.put("/api/settings", json={"maxArchivedSessions": 50}, headers=IDENTITY_HEADERS)
        put_resp = client.put(
            "/api/settings",
            json={"maxArchivedSessions": 0},
            headers=IDENTITY_HEADERS,
        )
        assert put_resp.status_code == 200
        assert put_resp.json()["maxArchivedSessions"] is None


def test_put_max_archived_clamps_to_ceiling():
    """An absurd value is clamped to the sanity ceiling, not stored verbatim."""
    from server.api.settings import _MAX_ARCHIVED_CEILING

    with _client() as client:
        put_resp = client.put(
            "/api/settings",
            json={"maxArchivedSessions": 10_000_000},
            headers=IDENTITY_HEADERS,
        )
        assert put_resp.status_code == 200
        assert put_resp.json()["maxArchivedSessions"] == _MAX_ARCHIVED_CEILING


# ---------------------------------------------------------------------------
# autoArchiveEnabled — global kill switch for completion auto-archive
# ---------------------------------------------------------------------------


def test_get_settings_auto_archive_enabled_defaults_true():
    """GET /api/settings returns autoArchiveEnabled true by default (column NULL)."""
    with _client() as client:
        resp = client.get("/api/settings", headers=IDENTITY_HEADERS)

    assert resp.status_code == 200
    assert resp.json()["autoArchiveEnabled"] is True


def test_put_auto_archive_enabled_false_round_trips():
    """PUT {autoArchiveEnabled: false} then GET → false (auto-archive off app-wide)."""
    with _client() as client:
        put_resp = client.put(
            "/api/settings",
            json={"autoArchiveEnabled": False},
            headers=IDENTITY_HEADERS,
        )
        assert put_resp.status_code == 200
        assert put_resp.json()["autoArchiveEnabled"] is False

        get_resp = client.get("/api/settings", headers=IDENTITY_HEADERS)
        assert get_resp.json()["autoArchiveEnabled"] is False


def test_put_auto_archive_enabled_back_to_true():
    """Turning it off then on again restores the default enabled state."""
    with _client() as client:
        client.put("/api/settings", json={"autoArchiveEnabled": False}, headers=IDENTITY_HEADERS)
        put_resp = client.put(
            "/api/settings",
            json={"autoArchiveEnabled": True},
            headers=IDENTITY_HEADERS,
        )
        assert put_resp.status_code == 200
        assert put_resp.json()["autoArchiveEnabled"] is True


# NOTE: prAutofixMode is no longer an /api/settings field. The pr-doctor mode
# moved onto the automation registry row (Automation id="pr-doctor", column
# `mode`); see test_pr_autofix_migration.py. No parallel state in AppSettings.

# ---------------------------------------------------------------------------
# archiveHome
# ---------------------------------------------------------------------------


def test_get_settings_archive_home_defaults_none():
    """GET /api/settings returns archiveHome null by default (server default path)."""
    with _client() as client:
        resp = client.get("/api/settings", headers=IDENTITY_HEADERS)

    assert resp.status_code == 200
    assert resp.json()["archiveHome"] is None


def test_put_archive_home_round_trips():
    """PUT {archiveHome: '/custom/path'} then GET → '/custom/path'."""
    with _client() as client:
        put_resp = client.put(
            "/api/settings",
            json={"archiveHome": "/custom/archive/path"},
            headers=IDENTITY_HEADERS,
        )
        assert put_resp.status_code == 200
        assert put_resp.json()["archiveHome"] == "/custom/archive/path"

        get_resp = client.get("/api/settings", headers=IDENTITY_HEADERS)
        assert get_resp.json()["archiveHome"] == "/custom/archive/path"


def test_put_archive_home_empty_string_clears_to_null():
    """PUT {archiveHome: ''} clears back to null (server default)."""
    with _client() as client:
        client.put(
            "/api/settings",
            json={"archiveHome": "/custom/archive/path"},
            headers=IDENTITY_HEADERS,
        )
        put_resp = client.put(
            "/api/settings",
            json={"archiveHome": ""},
            headers=IDENTITY_HEADERS,
        )
        assert put_resp.status_code == 200
        assert put_resp.json()["archiveHome"] is None

        get_resp = client.get("/api/settings", headers=IDENTITY_HEADERS)
        assert get_resp.json()["archiveHome"] is None


def test_put_archive_home_unchanged_when_omitted():
    """Omitting archiveHome from PUT body leaves the stored value intact."""
    with _client() as client:
        client.put(
            "/api/settings",
            json={"archiveHome": "/stored/path"},
            headers=IDENTITY_HEADERS,
        )
        # PUT something else, no archiveHome key
        client.put(
            "/api/settings",
            json={"thinkingCollapsed": False},
            headers=IDENTITY_HEADERS,
        )
        get_resp = client.get("/api/settings", headers=IDENTITY_HEADERS)
        assert get_resp.json()["archiveHome"] == "/stored/path"


# ---------------------------------------------------------------------------
# maxActiveSessions
# ---------------------------------------------------------------------------


def test_get_settings_max_active_defaults_none():
    """GET /api/settings returns maxActiveSessions null by default (UI shows placeholder)."""
    with _client() as client:
        resp = client.get("/api/settings", headers=IDENTITY_HEADERS)

    assert resp.status_code == 200
    assert resp.json()["maxActiveSessions"] is None


def test_put_max_active_round_trips():
    """PUT {maxActiveSessions: 3} then GET → 3."""
    with _client() as client:
        put_resp = client.put(
            "/api/settings",
            json={"maxActiveSessions": 3},
            headers=IDENTITY_HEADERS,
        )
        assert put_resp.status_code == 200
        assert put_resp.json()["maxActiveSessions"] == 3

        get_resp = client.get("/api/settings", headers=IDENTITY_HEADERS)
        assert get_resp.json()["maxActiveSessions"] == 3


def test_put_max_active_zero_clamps_to_one():
    """PUT {maxActiveSessions: 0} stores 1 (clamped to minimum)."""
    with _client() as client:
        put_resp = client.put(
            "/api/settings",
            json={"maxActiveSessions": 0},
            headers=IDENTITY_HEADERS,
        )
        assert put_resp.status_code == 200
        assert put_resp.json()["maxActiveSessions"] == 1


def test_cap_raised_helper():
    """Unit test _cap_raised: compares against effective default cap."""
    from server.api.settings import _cap_raised

    # (None, 3) → False: setting to default should not raise
    assert _cap_raised(None, 3) is False

    # (None, 7) → True: setting above default should raise
    assert _cap_raised(None, 7) is True

    # (5, 6) → True: increasing cap raises
    assert _cap_raised(5, 6) is True

    # (5, 5) → False: same cap does not raise
    assert _cap_raised(5, 5) is False

    # (None, 5) → False: setting to default should not raise
    assert _cap_raised(None, 5) is False


def test_put_max_active_null_to_default_no_raise():
    """PUT {maxActiveSessions: 3} from NULL should NOT trigger drain (equals default)."""
    with _client() as client:
        # First call: maxActiveSessions is NULL (default 5)
        get_resp = client.get("/api/settings", headers=IDENTITY_HEADERS)
        assert get_resp.json()["maxActiveSessions"] is None

        # PUT to 3 (below default of 5): should NOT raise, no drain needed
        put_resp = client.put(
            "/api/settings",
            json={"maxActiveSessions": 3},
            headers=IDENTITY_HEADERS,
        )
        assert put_resp.status_code == 200
        assert put_resp.json()["maxActiveSessions"] == 3

        # Verify the value persisted
        get_resp = client.get("/api/settings", headers=IDENTITY_HEADERS)
        assert get_resp.json()["maxActiveSessions"] == 3


# ---------------------------------------------------------------------------
# defaultPermissionMode ↔ client parity (S20)
# ---------------------------------------------------------------------------


def _client_mode_values() -> set[str]:
    """The `value:` strings in web/src/session/permissionModes.ts — the client's list."""
    src = (REPO_ROOT / "web" / "src" / "session" / "permissionModes.ts").read_text()
    return set(re.findall(r"value:\s*'([^']+)'", src))


def test_accepted_permission_modes_match_client_list():
    """S20: the server accepts exactly the modes the client offers."""
    from server.api.settings import ACCEPTED_PERMISSION_MODES

    assert set(ACCEPTED_PERMISSION_MODES) == _client_mode_values()
    assert len(ACCEPTED_PERMISSION_MODES) == 6


def test_put_bypass_permissions_round_trips():
    """`bypassPermissions` used to be silently dropped; now it persists."""
    with _client() as client:
        put = client.put("/api/settings", json={"defaultPermissionMode": "bypassPermissions"},
                         headers=IDENTITY_HEADERS)
        assert put.status_code == 200
        got = client.get("/api/settings", headers=IDENTITY_HEADERS).json()
    assert got["defaultPermissionMode"] == "bypassPermissions"


def test_put_unknown_permission_mode_is_422_not_silent():
    with _client() as client:
        resp = client.put("/api/settings", json={"defaultPermissionMode": "yolo"},
                          headers=IDENTITY_HEADERS)
        assert resp.status_code == 422
        assert resp.json()["detail"] == "invalid defaultPermissionMode"
        got = client.get("/api/settings", headers=IDENTITY_HEADERS).json()
    assert got["defaultPermissionMode"] == "interactive"


def test_put_unknown_tool_detail_is_422():
    with _client() as client:
        resp = client.put("/api/settings", json={"defaultToolDetail": "wat"}, headers=IDENTITY_HEADERS)
    assert resp.status_code == 422
    assert resp.json()["detail"] == "invalid defaultToolDetail"
