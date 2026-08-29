"""POST /api/sessions with host= must forward prefer= to create_session.

The API handler already calls:
    orch.create_session(prefer=getattr(body, "host", None), ...)

This test verifies that a `host` field in the request body reaches
FleetOrchestrator.create_session as `prefer=<host>`.

Harness pattern: _build_client injects a FakeOrch directly into app.state.sessions
(same approach as test_session_queue_api.py). The FakeOrch records the kwargs from
the most recent create_session call so we can assert against them.
"""
from __future__ import annotations

import os
import tempfile
import types
from unittest.mock import patch

from fastapi.testclient import TestClient

from server.app import create_app
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_H = {"Tailscale-User-Login": "testuser@example.com"}


def _make_env(db_url: str) -> dict:
    return {
        "CCSWITCH_DB_URL": db_url,
        "CCSWITCH_DEV": "1",
        "CCSWITCH_TAILNET_ALLOWLIST": "",
    }


def _make_record(sid: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=sid,
        name="Session",
        account="acct1",
        repo=None,
        host=None,
        status="idle",
        name_is_auto=True,
        detached=False,
        adopted_in_place=False,
        sub_agents=None,
        created_at=None,
        last_turn_at=None,
        account_pinned=False,
        model=None,
        active_goal=None,
        usage_unverified=False,
        updated_at_iso=None,
        rev_at=None,
        cwd=None,
    )


class FakeOrch:
    """Fake orchestrator that records the most recent create_session kwargs."""

    def __init__(self):
        self._records: dict[str, types.SimpleNamespace] = {}
        self.last_create_kwargs: dict = {}

    async def create_session(self, **kwargs):
        self.last_create_kwargs = kwargs
        rec = _make_record("sess-1")
        self._records[rec.id] = rec
        return rec

    async def list_sessions(self):
        return list(self._records.values())

    async def get_session(self, session_id: str):
        return self._records.get(session_id)

    async def latest_suggestions(self, sid):
        return []

    async def prs_for(self, sid):
        return []

    async def latest_summary(self, sid, *, viewing=False):
        return ""

    async def session_queue_positions(self) -> dict[str, int]:
        return {}

    async def all_prs(self):
        return []


def _build_client(fake_orch: FakeOrch, db_url: str) -> TestClient:
    env = _make_env(db_url)
    with patch.dict(os.environ, env):
        real_orch = SessionOrchestrator(
            sdk_factory=fake_sdk_factory(), poll_interval=9999.0, usage_fn=lambda n: None
        )
        app = create_app(orchestrator=real_orch)
    app.state.sessions = fake_orch
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Test: host field is forwarded as prefer= to create_session
# ---------------------------------------------------------------------------


def test_create_forwards_host_as_prefer():
    """POST with host='dobby' must reach create_session as prefer='dobby'."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_url = f"sqlite+aiosqlite:///{tf.name}"
    try:
        fake = FakeOrch()
        client = _build_client(fake, db_url)
        with patch.dict(os.environ, _make_env(db_url)), client:
            resp = client.post(
                "/api/sessions",
                json={"initialPrompt": "hi", "host": "dobby"},
                headers=_H,
            )
        assert resp.status_code == 201, resp.text
        assert fake.last_create_kwargs["prefer"] == "dobby"
    finally:
        os.unlink(tf.name)


def test_create_without_host_passes_prefer_none():
    """POST without host must reach create_session as prefer=None (auto-rank)."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_url = f"sqlite+aiosqlite:///{tf.name}"
    try:
        fake = FakeOrch()
        client = _build_client(fake, db_url)
        with patch.dict(os.environ, _make_env(db_url)), client:
            resp = client.post(
                "/api/sessions",
                json={"initialPrompt": "hi"},
                headers=_H,
            )
        assert resp.status_code == 201, resp.text
        assert fake.last_create_kwargs.get("prefer") is None
    finally:
        os.unlink(tf.name)
