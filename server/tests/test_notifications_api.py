"""API tests for /api/notifications (channels, rules, quiet hours, test) (#602)."""
from __future__ import annotations

import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from tests.helpers import temp_env

IDENTITY = {"Tailscale-User-Login": "testuser@example.com"}


@contextmanager
def _client():
    from server.sessions.orchestrator import SessionOrchestrator
    from server.app import create_app
    from fastapi.testclient import TestClient

    with temp_env(), tempfile.TemporaryDirectory() as d:
        db_url = f"sqlite+aiosqlite:///{Path(d) / 'notif.db'}"
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": db_url}):
            orch = SessionOrchestrator(poll_interval=9999.0)
            app = create_app(orchestrator=orch)
            # The router registration in server/app.py is wired by the caller of
            # this slice; include it here so the API surface is exercised end-to-end.
            _maybe_include_notifications_router(app)
            with TestClient(app, raise_server_exceptions=False) as c:
                yield c


def _maybe_include_notifications_router(app) -> None:
    """Include the notifications router if server/app.py has not already wired it.

    This backend slice ships the router module but does not edit app.py; the
    integrator adds ``app.include_router(notifications_router)``. Guard against
    double-registration so the test passes both before and after that wiring.
    """
    already = any(
        getattr(r, "path", "").startswith("/api/notifications") for r in app.router.routes
    )
    if not already:
        from server.api.notifications import router as notifications_router

        app.include_router(notifications_router)


# --------------------------------------------------------------------------- #
# Channels                                                                    #
# --------------------------------------------------------------------------- #
def test_create_and_list_channel():
    with _client() as c:
        r = c.post(
            "/api/notifications/channels",
            json={"name": "ops", "url": "https://ntfy.sh/ops"},
            headers=IDENTITY,
        )
        assert r.status_code == 200, r.text
        ch = r.json()
        assert ch["name"] == "ops"
        assert ch["kind"] == "webhook"
        assert ch["url"] == "https://ntfy.sh/ops"
        assert ch["enabled"] is True
        assert ch["id"]

        lst = c.get("/api/notifications/channels", headers=IDENTITY)
        assert lst.status_code == 200
        assert [x["id"] for x in lst.json()] == [ch["id"]]


def test_create_channel_rejects_bad_url():
    with _client() as c:
        r = c.post(
            "/api/notifications/channels",
            json={"name": "bad", "url": "ftp://nope"},
            headers=IDENTITY,
        )
        assert r.status_code == 422


def test_create_channel_rejects_ssrf_metadata_target():
    """SSRF defense-in-depth: the cloud metadata / link-local address is never a
    legitimate webhook target and is rejected even for the trusted operator."""
    with _client() as c:
        for url in ("http://169.254.169.254/latest/meta-data/",
                    "http://metadata.google.internal/computeMetadata/v1/"):
            r = c.post(
                "/api/notifications/channels",
                json={"name": "evil", "url": url},
                headers=IDENTITY,
            )
            assert r.status_code == 422, url


def test_delete_channel_nulls_referencing_rule():
    with _client() as c:
        ch = c.post(
            "/api/notifications/channels",
            json={"name": "ops", "url": "https://ntfy.sh/ops"},
            headers=IDENTITY,
        ).json()
        rule = c.post(
            "/api/notifications/rules",
            json={"eventClass": "automation_error", "action": "channel", "channelId": ch["id"]},
            headers=IDENTITY,
        ).json()
        assert rule["channelId"] == ch["id"]

        d = c.delete(f"/api/notifications/channels/{ch['id']}", headers=IDENTITY)
        assert d.status_code == 200 and d.json() == {"ok": True}

        rules = c.get("/api/notifications/rules", headers=IDENTITY).json()
        assert len(rules) == 1
        assert rules[0]["channelId"] is None


# --------------------------------------------------------------------------- #
# Rules                                                                       #
# --------------------------------------------------------------------------- #
def test_create_rule_channel_action_requires_existing_channel():
    with _client() as c:
        r = c.post(
            "/api/notifications/rules",
            json={"eventClass": "needs_input", "action": "channel", "channelId": "does-not-exist"},
            headers=IDENTITY,
        )
        assert r.status_code == 422


def test_create_rule_rejects_bad_event_class():
    with _client() as c:
        r = c.post(
            "/api/notifications/rules",
            json={"eventClass": "bogus", "action": "push"},
            headers=IDENTITY,
        )
        assert r.status_code == 422


def test_rules_listed_by_priority():
    with _client() as c:
        c.post(
            "/api/notifications/rules",
            json={"eventClass": "needs_input", "action": "push", "priority": 50},
            headers=IDENTITY,
        )
        c.post(
            "/api/notifications/rules",
            json={"eventClass": "pr_merged", "action": "push", "priority": 10},
            headers=IDENTITY,
        )
        rules = c.get("/api/notifications/rules", headers=IDENTITY).json()
        assert [r["priority"] for r in rules] == [10, 50]


# --------------------------------------------------------------------------- #
# Quiet hours                                                                 #
# --------------------------------------------------------------------------- #
def test_quiet_hours_round_trip():
    with _client() as c:
        assert c.get("/api/notifications/quiet-hours", headers=IDENTITY).json() == {
            "start": None,
            "end": None,
        }
        put = c.put(
            "/api/notifications/quiet-hours",
            json={"start": "23:00", "end": "07:00"},
            headers=IDENTITY,
        )
        assert put.status_code == 200
        assert put.json() == {"start": "23:00", "end": "07:00"}
        assert c.get("/api/notifications/quiet-hours", headers=IDENTITY).json() == {
            "start": "23:00",
            "end": "07:00",
        }


def test_quiet_hours_rejects_bad_time():
    with _client() as c:
        r = c.put(
            "/api/notifications/quiet-hours",
            json={"start": "25:99", "end": "07:00"},
            headers=IDENTITY,
        )
        assert r.status_code == 422


# --------------------------------------------------------------------------- #
# Test / preview                                                              #
# --------------------------------------------------------------------------- #
def test_test_endpoint_returns_routing_decision():
    with _client() as c:
        ch = c.post(
            "/api/notifications/channels",
            json={"name": "ops", "url": "https://ntfy.sh/ops"},
            headers=IDENTITY,
        ).json()
        err_rule = c.post(
            "/api/notifications/rules",
            json={"eventClass": "automation_error", "action": "channel",
                  "channelId": ch["id"], "priority": 10},
            headers=IDENTITY,
        ).json()
        c.post(
            "/api/notifications/rules",
            json={"eventClass": "needs_input", "action": "push", "priority": 20},
            headers=IDENTITY,
        )

        err = c.post(
            "/api/notifications/test", json={"eventClass": "automation_error"}, headers=IDENTITY
        ).json()
        assert err["action"] == "channel"
        assert err["channelId"] == ch["id"]
        assert err["deliver"] is True
        assert err["suppressedByQuiet"] is False
        assert err["matchedRuleId"] == err_rule["id"]

        inp = c.post(
            "/api/notifications/test", json={"eventClass": "needs_input"}, headers=IDENTITY
        ).json()
        assert inp["action"] == "push" and inp["deliver"] is True

        # No rule matches session_ended, and rules exist → silent ignore.
        ended = c.post(
            "/api/notifications/test", json={"eventClass": "session_ended"}, headers=IDENTITY
        ).json()
        assert ended["action"] == "ignore" and ended["deliver"] is False


def test_unauthenticated_rejected():
    with _client() as c:
        assert c.get("/api/notifications/channels").status_code == 401
