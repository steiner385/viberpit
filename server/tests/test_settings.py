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
        with patch.dict(os.environ, {"VIBERPIT_DB_URL": db_url}):
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


def test_put_archive_home_is_409_and_unchanged_when_moves_fail(monkeypatch):
    """S16: a partial move must not leave the DB pointing at a path the files are not in."""
    import server.api.settings as settings_api

    async def fake_migrate(orch, db, old_home, new_home):
        return (2, 1, 3)

    monkeypatch.setattr(settings_api, "_migrate_archive_home", fake_migrate)
    with _client() as client:
        resp = client.put("/api/settings", json={"archiveHome": "/mnt/nowhere"}, headers=IDENTITY_HEADERS)
        assert resp.status_code == 409
        assert resp.json()["detail"] == {"code": "archive_move_failed", "moved": 2, "failed": 1, "total": 3}
        got = client.get("/api/settings", headers=IDENTITY_HEADERS).json()
    assert got["archiveHome"] is None


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


def test_put_max_active_below_one_is_422():
    """PUT {maxActiveSessions: 0} is refused, not silently clamped (spec 2026-09-20 §7)."""
    with _client() as client:
        resp = client.put("/api/settings", headers=IDENTITY_HEADERS, json={"maxActiveSessions": 0})
        assert resp.status_code == 422, resp.text
        assert client.get("/api/settings", headers=IDENTITY_HEADERS).json()["maxActiveSessions"] is None


def test_put_max_active_null_clears_the_attention_ceiling():
    """An explicit null clears (the web sends null for a blank field); an OMITTED key leaves it alone."""
    with _client() as client:
        client.put("/api/settings", headers=IDENTITY_HEADERS, json={"maxActiveSessions": 3})
        assert client.get("/api/settings", headers=IDENTITY_HEADERS).json()["maxActiveSessions"] == 3
        client.put("/api/settings", headers=IDENTITY_HEADERS, json={"thinkingCollapsed": False})
        assert client.get("/api/settings", headers=IDENTITY_HEADERS).json()["maxActiveSessions"] == 3
        resp = client.put("/api/settings", headers=IDENTITY_HEADERS, json={"maxActiveSessions": None})
        assert resp.status_code == 200, resp.text
        assert resp.json()["maxActiveSessions"] is None


def test_put_max_active_null_to_default_no_raise():
    """PUT {maxActiveSessions: 3} from NULL should NOT trigger drain (equals default),
    but a later raise past a physical ceiling of two capped hosts must (M7)."""
    from unittest.mock import AsyncMock, patch

    with _client() as client:
        # First call: maxActiveSessions is NULL (default 5)
        get_resp = client.get("/api/settings", headers=IDENTITY_HEADERS)
        assert get_resp.json()["maxActiveSessions"] is None

        for name, cap in (("dobby", 4), ("rocky", 6)):
            r = client.post("/api/fleet/hosts", headers=IDENTITY_HEADERS,
                            json={"name": name, "endpoint": f"https://{name}:9443", "maxSessions": cap})
            assert r.status_code == 201, r.text

        with patch.object(client.app.state.sessions, "maybe_start_queued", new_callable=AsyncMock) as drain:
            # PUT to 3 (below default of 5, and below the 10 physical ceiling):
            # should NOT raise, no drain needed.
            put_resp = client.put(
                "/api/settings",
                json={"maxActiveSessions": 3},
                headers=IDENTITY_HEADERS,
            )
            assert put_resp.status_code == 200
            assert put_resp.json()["maxActiveSessions"] == 3
            drain.assert_not_awaited()

            # Verify the value persisted
            get_resp = client.get("/api/settings", headers=IDENTITY_HEADERS)
            assert get_resp.json()["maxActiveSessions"] == 3

            # PUT to 8 (raises the effective cap 3 -> 8): must drain exactly once.
            put_resp = client.put(
                "/api/settings",
                json={"maxActiveSessions": 8},
                headers=IDENTITY_HEADERS,
            )
            assert put_resp.status_code == 200
            drain.assert_awaited_once()


def test_put_max_active_below_one_after_a_valid_value_leaves_it_intact():
    """A rejected PUT (0 is invalid) must not disturb the last valid value."""
    with _client() as client:
        put_resp = client.put("/api/settings", headers=IDENTITY_HEADERS, json={"maxActiveSessions": 3})
        assert put_resp.status_code == 200
        assert put_resp.json()["maxActiveSessions"] == 3

        resp = client.put("/api/settings", headers=IDENTITY_HEADERS, json={"maxActiveSessions": 0})
        assert resp.status_code == 422, resp.text

        get_resp = client.get("/api/settings", headers=IDENTITY_HEADERS)
        assert get_resp.json()["maxActiveSessions"] == 3


def test_put_with_an_invalid_sibling_field_persists_nothing_and_does_not_drain():
    """The cap commit must come AFTER every validation: a 422 on another field
    leaves the cap untouched and fires no drain."""
    from unittest.mock import AsyncMock, patch

    with _client() as client:
        for name, cap in (("dobby", 4), ("rocky", 6)):
            client.post("/api/fleet/hosts", headers=IDENTITY_HEADERS,
                        json={"name": name, "endpoint": f"https://{name}:9443", "maxSessions": cap})
        client.put("/api/settings", headers=IDENTITY_HEADERS, json={"maxActiveSessions": 3})
        with patch.object(client.app.state.sessions, "maybe_start_queued", new_callable=AsyncMock) as drain:
            resp = client.put("/api/settings", headers=IDENTITY_HEADERS,
                              json={"maxActiveSessions": 8, "modelApprovalAction": "bogus"})
            assert resp.status_code == 422, resp.text
            drain.assert_not_awaited()
        assert client.get("/api/settings", headers=IDENTITY_HEADERS).json()["maxActiveSessions"] == 3


# ---------------------------------------------------------------------------
# effectiveDefaults.globalCap (spec 2026-09-20 D3)
# ---------------------------------------------------------------------------


def test_effective_global_cap_derives_from_enabled_host_caps():
    with _client() as client:
        gc = client.get("/api/settings", headers=IDENTITY_HEADERS).json()["effectiveDefaults"]["globalCap"]
        assert gc == {"physical": None, "attention": None, "effective": 5, "binding": "default", "hosts": []}

        for name, cap in (("dobby", 4), ("rocky", 6)):
            r = client.post("/api/fleet/hosts", headers=IDENTITY_HEADERS,
                            json={"name": name, "endpoint": f"https://{name}:9443", "maxSessions": cap})
            assert r.status_code == 201, r.text
        gc = client.get("/api/settings", headers=IDENTITY_HEADERS).json()["effectiveDefaults"]["globalCap"]
        assert gc["physical"] == 10 and gc["effective"] == 10 and gc["binding"] == "physical"
        assert gc["hosts"] == [{"name": "dobby", "cap": 4}, {"name": "rocky", "cap": 6}]

        client.put("/api/settings", headers=IDENTITY_HEADERS, json={"maxActiveSessions": 6})
        gc = client.get("/api/settings", headers=IDENTITY_HEADERS).json()["effectiveDefaults"]["globalCap"]
        assert (gc["attention"], gc["effective"], gc["binding"]) == (6, 6, "attention")


def test_managed_lists_the_capacity_env_flags():
    with _client() as client:
        keys = {m["key"] for m in client.get("/api/settings", headers=IDENTITY_HEADERS).json()["managed"]}
    for env in ("VIBERPIT_CHILD_SESSION_CAP",
                "VIBERPIT_STT_MAX_CONCURRENT", "VIBERPIT_CAPACITY_GOVERNOR_ENABLED"):
        assert env in keys, env


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


# ---------------------------------------------------------------------------
# effectiveDefaults / acceptedPermissionModes / managed / about (S28, S30)
# ---------------------------------------------------------------------------


def test_get_settings_exposes_effective_defaults_and_accepted_modes(tmp_path):
    # The root must be a real temp dir, not an arbitrary literal: entering the
    # client starts the app lifespan, whose orchestrator fires the worktree
    # janitor at whatever the configured repo root says (#1014).
    repo_root = str(tmp_path / "repos")
    with temp_env(), patch.dict(os.environ, {
        "VIBERPIT_MODEL_APPROVAL_ACTION": "block",
        "VIBERPIT_MODEL_APPROVAL_DENYLIST": "fable,opus",
        "VIBERPIT_REPO_ROOT": repo_root,
    }):
        with _client() as client:
            data = client.get("/api/settings", headers=IDENTITY_HEADERS).json()
    assert data["acceptedPermissionModes"][0] == "interactive"
    assert len(data["acceptedPermissionModes"]) == 6
    eff = data["effectiveDefaults"]
    assert eff == {
        "repoRoot": repo_root,
        "archiveHome": None,
        "modelApprovalAction": "block",
        "modelApprovalDenylist": "fable,opus",
        "globalCap": {"physical": None, "attention": None, "effective": 5, "binding": "default", "hosts": []},
    }


def test_get_settings_managed_is_an_allowlist_with_no_secrets():
    from server.version import running_git_sha

    # running_git_sha() is resolved once and cached for the process's life
    # (see server/version.py); clear it so this test's VIBERPIT_GIT_SHA override
    # is actually observed, and clear it again after so later tests aren't stuck
    # with this test's value.
    running_git_sha.cache_clear()
    try:
        with temp_env(), patch.dict(os.environ, {
            "VIBERPIT_SELF_MODEL_SWITCH_CAP": "7",
            "VIBERPIT_MASTER_KEY": "sekrit",
            "VIBERPIT_GIT_SHA": "abc1234",
            "VIBERPIT_HOST_NAME": "rocky",
        }):
            with _client() as client:
                data = client.get("/api/settings", headers=IDENTITY_HEADERS).json()
    finally:
        running_git_sha.cache_clear()
    keys = [m["key"] for m in data["managed"]]
    assert "VIBERPIT_MASTER_KEY" not in keys
    assert "VIBERPIT_TAILNET_ALLOWLIST" not in keys
    by_key = {m["key"]: m for m in data["managed"]}
    assert by_key["VIBERPIT_SELF_MODEL_SWITCH_CAP"]["value"] == 7
    assert by_key["VIBERPIT_SELF_MODEL_SWITCH_CAP"]["description"]
    assert data["about"] == {"gitSha": "abc1234", "hostName": "rocky"}


def test_get_settings_about_falls_back_to_shared_identity_helpers():
    """S30: with no operator overrides, about.hostName/gitSha come straight from
    server.hostname.self_host_name / server.version.running_git_sha — the same
    functions the deploy controller and /healthz use — not a re-implementation."""
    from server.hostname import self_host_name
    from server.version import running_git_sha

    running_git_sha.cache_clear()
    try:
        with temp_env(), patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VIBERPIT_HOST_NAME", None)
            os.environ.pop("VIBERPIT_GIT_SHA", None)
            with _client() as client:
                data = client.get("/api/settings", headers=IDENTITY_HEADERS).json()
            expected_host = self_host_name()
            expected_sha = running_git_sha()
    finally:
        running_git_sha.cache_clear()
    assert data["about"]["hostName"] == expected_host
    assert data["about"]["gitSha"] in (None, expected_sha)


def test_put_settings_returns_the_same_shape_as_get():
    with _client() as client:
        put = client.put("/api/settings", json={"thinkingCollapsed": False}, headers=IDENTITY_HEADERS).json()
        get = client.get("/api/settings", headers=IDENTITY_HEADERS).json()
    assert set(put) == set(get)
    assert "effectiveDefaults" in put
