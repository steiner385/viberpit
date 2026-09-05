"""Tests for the queue-stall reason surfaced on GET /api/sessions.

The 2026-08-22 deadlock left 15 sessions queued with capacity fully occupied by
finished-but-exempt-from-archive sessions, and the UI said nothing — finding the
cause took a DB query, a source read, and a py-spy dump. This endpoint should say
why the queue isn't draining, using the numbers, whenever it can't.

Mirrors the fixture/client conventions in test_session_queue_api.py (fake facade
+ real orchestrator app + a temp-file DB so AppSettings.max_active_sessions reads
hit real SQLAlchemy machinery, not a stub).
"""
from __future__ import annotations

import os
import tempfile
import types
from unittest.mock import patch

from fastapi.testclient import TestClient

from server.api.sessions import _record_to_dict
from server.app import create_app
from server.db.models import DEFAULT_MAX_ACTIVE_SESSIONS
from server.sessions.engine import AgentEngine
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory

_H = {"Tailscale-User-Login": "testuser@example.com"}


def _make_env(db_url: str) -> dict:
    return {
        "CCSWITCH_DB_URL": db_url,
        "CCSWITCH_DEV": "1",
        "CCSWITCH_TAILNET_ALLOWLIST": "",
    }


def _make_record(sid: str, status: str = "idle") -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=sid,
        name=f"Session {sid}",
        account="acct1",
        engine=AgentEngine.CLAUDE_CODE,
        engine_session_id=None,
        reasoning_effort=None,
        repo=None,
        status=status,
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


class _FakeOrch:
    """Minimal facade implementing list_sessions for API tests (no queue RPCs needed)."""

    def __init__(self, records=None, positions=None):
        self._records: dict[str, types.SimpleNamespace] = {r.id: r for r in (records or [])}
        self._positions: dict[str, int] = dict(positions or {})

    async def list_sessions(self):
        return list(self._records.values())

    async def latest_suggestions(self, sid):
        return []

    async def prs_for(self, sid):
        return []

    async def latest_summary(self, sid, *, viewing=False):
        return ""

    async def session_queue_positions(self) -> dict[str, int]:
        return dict(self._positions)

    async def get_session(self, session_id: str):
        return self._records.get(session_id)


def _build_client(fake_orch: _FakeOrch, db_url: str) -> TestClient:
    env = _make_env(db_url)
    with patch.dict(os.environ, env):
        real_orch = SessionOrchestrator(
            sdk_factory=fake_sdk_factory(), poll_interval=9999.0, usage_fn=lambda n: None
        )
        app = create_app(orchestrator=real_orch)
    app.state.sessions = fake_orch
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Unit: _record_to_dict
# ---------------------------------------------------------------------------


def test_record_to_dict_includes_queue_stall_reason_when_passed():
    rec = _make_record("q1", status="queued")
    reason = {"code": "capacity_full", "message": "stalled", "capOccupying": 5,
               "cap": 5, "queuedCount": 1}
    d = _record_to_dict(rec, queue_stall_reason=reason)
    assert d["queueStallReason"] == reason


def test_record_to_dict_queue_stall_reason_none_by_default():
    rec = _make_record("s1", status="idle")
    d = _record_to_dict(rec)
    assert d.get("queueStallReason") is None


# ---------------------------------------------------------------------------
# Integration: GET /api/sessions
# ---------------------------------------------------------------------------


def test_queue_stalled_on_capacity_reports_reason_with_numbers():
    """Queue non-empty AND capacity full (== DEFAULT_MAX_ACTIVE_SESSIONS) → a
    queued session's payload names the cause and carries the numbers."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_url = f"sqlite+aiosqlite:///{tf.name}"
    try:
        cap = DEFAULT_MAX_ACTIVE_SESSIONS
        occupying = [_make_record(f"a{i}", status="in_progress") for i in range(cap)]
        queued = [_make_record("q1", status="queued")]
        fake = _FakeOrch(records=occupying + queued, positions={"q1": 1})
        client = _build_client(fake, db_url)
        with patch.dict(os.environ, _make_env(db_url)), client:
            resp = client.get("/api/sessions", headers=_H)
        assert resp.status_code == 200, resp.text
        sessions = {s["id"]: s for s in resp.json()}
        reason = sessions["q1"]["queueStallReason"]
        assert reason is not None
        assert reason["capOccupying"] == cap
        assert reason["cap"] == cap
        assert reason["queuedCount"] == 1
        assert str(cap) in reason["message"]
        # Non-queued sessions never carry a stall reason.
        assert sessions["a0"]["queueStallReason"] is None
    finally:
        os.unlink(tf.name)


def test_queue_nonempty_with_capacity_available_reports_no_reason():
    """Queue non-empty but capacity is NOT full → nothing is wrong, no reason."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_url = f"sqlite+aiosqlite:///{tf.name}"
    try:
        cap = DEFAULT_MAX_ACTIVE_SESSIONS
        occupying = [_make_record(f"a{i}", status="in_progress") for i in range(cap - 1)]
        queued = [_make_record("q1", status="queued")]
        fake = _FakeOrch(records=occupying + queued, positions={"q1": 1})
        client = _build_client(fake, db_url)
        with patch.dict(os.environ, _make_env(db_url)), client:
            resp = client.get("/api/sessions", headers=_H)
        assert resp.status_code == 200, resp.text
        sessions = {s["id"]: s for s in resp.json()}
        assert sessions["q1"]["queueStallReason"] is None
    finally:
        os.unlink(tf.name)


def test_empty_queue_reports_no_reason():
    """No queued sessions at all → no stall reason, even if capacity is full."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_url = f"sqlite+aiosqlite:///{tf.name}"
    try:
        cap = DEFAULT_MAX_ACTIVE_SESSIONS
        occupying = [_make_record(f"a{i}", status="in_progress") for i in range(cap)]
        fake = _FakeOrch(records=occupying, positions={})
        client = _build_client(fake, db_url)
        with patch.dict(os.environ, _make_env(db_url)), client:
            resp = client.get("/api/sessions", headers=_H)
        assert resp.status_code == 200, resp.text
        sessions = resp.json()
        assert all(s["queueStallReason"] is None for s in sessions)
    finally:
        os.unlink(tf.name)


def test_response_is_backward_compatible():
    """Every field the endpoint returned before this change is still present
    with the same type — the field addition is purely additive."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_url = f"sqlite+aiosqlite:///{tf.name}"
    try:
        rec = _make_record("s1", status="idle")
        fake = _FakeOrch(records=[rec], positions={})
        client = _build_client(fake, db_url)
        with patch.dict(os.environ, _make_env(db_url)), client:
            resp = client.get("/api/sessions", headers=_H)
        assert resp.status_code == 200, resp.text
        s = resp.json()[0]
        expected_types = {
            "id": str, "name": str, "accountName": str, "status": str,
            "nameIsAuto": bool, "detached": bool, "adoptedInPlace": bool,
            "subAgents": list, "accountPinned": bool, "latestSuggestions": list,
            "prs": list, "usageUnverified": bool, "interruptedByRestart": bool,
            "summary": str,
        }
        for field, typ in expected_types.items():
            assert field in s, f"missing pre-existing field {field!r}"
            assert isinstance(s[field], typ), (
                f"{field!r} changed type: expected {typ}, got {type(s[field])}")
        # Nullable pre-existing fields must still be present (value may be None).
        for field in (
            "repo", "host", "origin", "originRef", "assistedBy", "createdAt",
            "updatedAt", "revAt", "model", "activeGoal", "inferredGoal",
            "effectiveGoal", "goalSource", "autopilotPaused",
            "autopilotCooldownUntil", "autoArchiveOptOut", "queuePosition",
        ):
            assert field in s, f"missing pre-existing field {field!r}"
        # And the new field is additive, not a replacement.
        assert "queueStallReason" in s
    finally:
        os.unlink(tf.name)
