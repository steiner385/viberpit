"""Tests for queue-move/cancel endpoints and queuePosition in session payloads.

Uses an in-process orchestrator adapter (fake facade) to avoid needing a full
SessionOrchestrator. A temp-file DB URL avoids :memory: seed-visibility flakes.
"""
from __future__ import annotations

import os
import tempfile
import types
from unittest.mock import patch

from fastapi.testclient import TestClient

from server.api.sessions import _record_to_dict
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


def _make_record(sid: str, status: str = "idle") -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=sid,
        name=f"Session {sid}",
        account="acct1",
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


# ---------------------------------------------------------------------------
# Unit tests: _record_to_dict queuePosition field
# ---------------------------------------------------------------------------


def test_record_to_dict_includes_queue_position():
    """queuePosition is exposed when passed explicitly."""
    rec = _make_record("s1", status="queued")
    d = _record_to_dict(rec, queue_position=1)
    assert d["queuePosition"] == 1


def test_record_to_dict_queue_position_none_by_default():
    """queuePosition defaults to None for non-queued sessions."""
    rec = _make_record("s1", status="idle")
    d = _record_to_dict(rec)
    assert d.get("queuePosition") is None


# ---------------------------------------------------------------------------
# Fake facade for HTTP integration tests
# ---------------------------------------------------------------------------


class _FakeOrch:
    """Minimal facade implementing queue + list operations for API tests."""

    def __init__(self, records=None, positions=None):
        self._records: dict[str, types.SimpleNamespace] = {r.id: r for r in (records or [])}
        self._positions: dict[str, int] = dict(positions or {})
        # Track calls for assertions
        self.move_calls: list[tuple] = []
        self.cancel_calls: list[str] = []

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

    async def session_queue_move(self, session_id: str, op: str, position: int | None = None) -> bool:
        self.move_calls.append((session_id, op, position))
        return session_id in self._positions

    async def session_queue_cancel(self, session_id: str) -> bool:
        self.cancel_calls.append(session_id)
        if session_id not in self._positions:
            return False
        # Simulate the orchestrator: mark ended and remove from queue
        del self._positions[session_id]
        if session_id in self._records:
            self._records[session_id].status = "ended"
        return True


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
# Integration: GET /api/sessions — queuePosition in list payload
# ---------------------------------------------------------------------------


def test_list_sessions_includes_queue_position_for_queued():
    """A queued session's list payload has queuePosition: 1."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_url = f"sqlite+aiosqlite:///{tf.name}"
    try:
        rec_queued = _make_record("q1", status="queued")
        rec_live = _make_record("l1", status="in_progress")
        fake = _FakeOrch(
            records=[rec_queued, rec_live],
            positions={"q1": 1},
        )
        client = _build_client(fake, db_url)
        with patch.dict(os.environ, _make_env(db_url)), client:
            resp = client.get("/api/sessions", headers=_H)
        assert resp.status_code == 200, resp.text
        sessions = {s["id"]: s for s in resp.json()}
        assert sessions["q1"]["queuePosition"] == 1
        assert sessions["l1"]["queuePosition"] is None
    finally:
        os.unlink(tf.name)


# ---------------------------------------------------------------------------
# Integration: GET /api/sessions/{id} — queuePosition in detail payload
# ---------------------------------------------------------------------------


def test_get_session_includes_queue_position():
    """Detail endpoint also includes queuePosition."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_url = f"sqlite+aiosqlite:///{tf.name}"
    try:
        rec = _make_record("q1", status="queued")
        fake = _FakeOrch(records=[rec], positions={"q1": 1})
        client = _build_client(fake, db_url)
        with patch.dict(os.environ, _make_env(db_url)), client:
            resp = client.get("/api/sessions/q1", headers=_H)
        assert resp.status_code == 200, resp.text
        assert resp.json()["queuePosition"] == 1
    finally:
        os.unlink(tf.name)


def test_get_session_queue_position_null_for_live():
    """Live session detail has queuePosition: null."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_url = f"sqlite+aiosqlite:///{tf.name}"
    try:
        rec = _make_record("l1", status="in_progress")
        fake = _FakeOrch(records=[rec], positions={})
        client = _build_client(fake, db_url)
        with patch.dict(os.environ, _make_env(db_url)), client:
            resp = client.get("/api/sessions/l1", headers=_H)
        assert resp.status_code == 200, resp.text
        assert resp.json()["queuePosition"] is None
    finally:
        os.unlink(tf.name)


# ---------------------------------------------------------------------------
# Integration: POST /api/sessions/{id}/queue-move
# ---------------------------------------------------------------------------


def test_queue_move_op_top_returns_ok():
    """POST queue-move with op=top → 200 {"ok": true}."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_url = f"sqlite+aiosqlite:///{tf.name}"
    try:
        rec = _make_record("q1", status="queued")
        fake = _FakeOrch(records=[rec], positions={"q1": 2})
        client = _build_client(fake, db_url)
        with patch.dict(os.environ, _make_env(db_url)), client:
            resp = client.post("/api/sessions/q1/queue-move", json={"op": "top"}, headers=_H)
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True}
        assert fake.move_calls == [("q1", "top", None)]
    finally:
        os.unlink(tf.name)


def test_queue_move_position_op_passes_position():
    """POST queue-move with op=position passes position kwarg."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_url = f"sqlite+aiosqlite:///{tf.name}"
    try:
        rec = _make_record("q2", status="queued")
        fake = _FakeOrch(records=[rec], positions={"q2": 1})
        client = _build_client(fake, db_url)
        with patch.dict(os.environ, _make_env(db_url)), client:
            resp = client.post(
                "/api/sessions/q2/queue-move", json={"op": "position", "position": 3}, headers=_H
            )
        assert resp.status_code == 200, resp.text
        assert fake.move_calls == [("q2", "position", 3)]
    finally:
        os.unlink(tf.name)


def test_queue_move_on_non_queued_session_returns_404():
    """POST queue-move on a session not in the queue → 404."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_url = f"sqlite+aiosqlite:///{tf.name}"
    try:
        rec = _make_record("l1", status="in_progress")
        fake = _FakeOrch(records=[rec], positions={})
        client = _build_client(fake, db_url)
        with patch.dict(os.environ, _make_env(db_url)), client:
            resp = client.post("/api/sessions/l1/queue-move", json={"op": "up"}, headers=_H)
        assert resp.status_code == 404, resp.text
    finally:
        os.unlink(tf.name)


def test_queue_move_on_unknown_session_returns_404():
    """POST queue-move on an unknown session id → 404."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_url = f"sqlite+aiosqlite:///{tf.name}"
    try:
        fake = _FakeOrch(records=[], positions={})
        client = _build_client(fake, db_url)
        with patch.dict(os.environ, _make_env(db_url)), client:
            resp = client.post(
                "/api/sessions/no-such-id/queue-move", json={"op": "up"}, headers=_H
            )
        assert resp.status_code == 404, resp.text
    finally:
        os.unlink(tf.name)


def test_queue_move_invalid_op_returns_422():
    """POST queue-move with invalid op → 422 (FastAPI validation error)."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_url = f"sqlite+aiosqlite:///{tf.name}"
    try:
        rec = _make_record("q1", status="queued")
        fake = _FakeOrch(records=[rec], positions={"q1": 1})
        client = _build_client(fake, db_url)
        with patch.dict(os.environ, _make_env(db_url)), client:
            resp = client.post(
                "/api/sessions/q1/queue-move", json={"op": "sideways"}, headers=_H
            )
        assert resp.status_code == 422, resp.text
    finally:
        os.unlink(tf.name)


# ---------------------------------------------------------------------------
# Integration: DELETE /api/sessions/{id}/queue
# ---------------------------------------------------------------------------


def test_queue_cancel_returns_204():
    """DELETE /queue on a queued session → 204 and status becomes ended."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_url = f"sqlite+aiosqlite:///{tf.name}"
    try:
        rec = _make_record("q1", status="queued")
        fake = _FakeOrch(records=[rec], positions={"q1": 1})
        client = _build_client(fake, db_url)
        with patch.dict(os.environ, _make_env(db_url)), client:
            resp = client.delete("/api/sessions/q1/queue", headers=_H)
        assert resp.status_code == 204, resp.text
        assert fake.cancel_calls == ["q1"]
        assert fake._records["q1"].status == "ended"
    finally:
        os.unlink(tf.name)


def test_queue_cancel_on_non_queued_returns_404():
    """DELETE /queue on a non-queued session → 404."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_url = f"sqlite+aiosqlite:///{tf.name}"
    try:
        rec = _make_record("l1", status="in_progress")
        fake = _FakeOrch(records=[rec], positions={})
        client = _build_client(fake, db_url)
        with patch.dict(os.environ, _make_env(db_url)), client:
            resp = client.delete("/api/sessions/l1/queue", headers=_H)
        assert resp.status_code == 404, resp.text
    finally:
        os.unlink(tf.name)


def test_queue_cancel_second_call_returns_404():
    """Second DELETE /queue on same session → 404 (already cancelled)."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_url = f"sqlite+aiosqlite:///{tf.name}"
    try:
        rec = _make_record("q1", status="queued")
        fake = _FakeOrch(records=[rec], positions={"q1": 1})
        client = _build_client(fake, db_url)
        with patch.dict(os.environ, _make_env(db_url)), client:
            resp1 = client.delete("/api/sessions/q1/queue", headers=_H)
            resp2 = client.delete("/api/sessions/q1/queue", headers=_H)
        assert resp1.status_code == 204, resp1.text
        assert resp2.status_code == 404, resp2.text
    finally:
        os.unlink(tf.name)


def test_queue_cancel_unknown_session_returns_404():
    """DELETE /queue on an unknown session → 404."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_url = f"sqlite+aiosqlite:///{tf.name}"
    try:
        fake = _FakeOrch(records=[], positions={})
        client = _build_client(fake, db_url)
        with patch.dict(os.environ, _make_env(db_url)), client:
            resp = client.delete("/api/sessions/no-such/queue", headers=_H)
        assert resp.status_code == 404, resp.text
    finally:
        os.unlink(tf.name)
