"""Tests for queue-move/cancel endpoints and queuePosition in session payloads.

Uses an in-process orchestrator adapter (fake facade) to avoid needing a full
SessionOrchestrator. A temp-file DB URL avoids :memory: seed-visibility flakes.
"""
from __future__ import annotations

import os
import tempfile
import types
from unittest.mock import patch

import pytest

from fastapi.testclient import TestClient

from server.api.sessions import _record_to_dict
from server.app import create_app
from server.ipc.client import DaemonError, DaemonUnavailable
from server.sessions.engine import AgentEngine
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_H = {"Tailscale-User-Login": "testuser@example.com"}


def _make_env(db_url: str) -> dict:
    return {
        "VIBERPIT_DB_URL": db_url,
        "VIBERPIT_DEV": "1",
        "VIBERPIT_TAILNET_ALLOWLIST": "",
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


# ---------------------------------------------------------------------------
# Integration: POST /api/sessions when the fleet is full — queued, not an error
# ---------------------------------------------------------------------------


class _QueueingOrch(_FakeOrch):
    """A fleet whose every host is full: create_session answers a queued record."""

    async def create_session(self, **kwargs):
        rec = _make_record("q-new", status="queued")
        self._records[rec.id] = rec
        self._positions[rec.id] = len(self._positions) + 1
        return rec


def test_create_when_every_host_is_full_answers_queued_with_a_position():
    """The UI must get the queued state and its position, never the old
    "no enabled, healthy host with capacity" 409 (see
    test_fleet_queue_on_host_capacity for the edge turning that refusal into a
    queued create)."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_url = f"sqlite+aiosqlite:///{tf.name}"
    try:
        fake = _QueueingOrch(positions={"q-old": 1})
        client = _build_client(fake, db_url)
        with patch.dict(os.environ, _make_env(db_url)), client:
            resp = client.post("/api/sessions", json={"initialPrompt": "hi"}, headers=_H)
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["status"] == "queued"
        assert body["queuePosition"] == 2
        assert "error" not in body
    finally:
        os.unlink(tf.name)


# ---------------------------------------------------------------------------
# Integration: POST /api/sessions/{id}/start-now — "Start now" (2026-10-08)
# ---------------------------------------------------------------------------


class _StartNowOrch(_FakeOrch):
    """start_queued_now answers ``outcome``, or raises ``raises`` when set."""

    def __init__(self, *a, outcome: str = "started", raises: Exception | None = None, **k):
        super().__init__(*a, **k)
        self.outcome, self.raises = outcome, raises
        self.start_now_calls: list[str] = []

    async def start_queued_now(self, session_id: str) -> str:
        self.start_now_calls.append(session_id)
        if self.raises is not None:
            raise self.raises
        return self.outcome


def _audit_rows(db_path: str, type_: str) -> list[tuple]:
    import sqlite3
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT identity, detail FROM audit_events WHERE type = ?", (type_,)).fetchall()


def test_start_now_on_unqueued_session_returns_404():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_url = f"sqlite+aiosqlite:///{tf.name}"
    try:
        fake = _StartNowOrch(records=[_make_record("l1", status="idle")])
        client = _build_client(fake, db_url)
        with patch.dict(os.environ, _make_env(db_url)), client:
            resp = client.post("/api/sessions/l1/start-now", headers=_H)
            unknown = client.post("/api/sessions/nope/start-now", headers=_H)
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"]["code"] == "not_queued"
        assert unknown.status_code == 404, unknown.text
        assert fake.start_now_calls == []
    finally:
        os.unlink(tf.name)


def test_start_now_started_returns_ok_and_audits():
    import json
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_url = f"sqlite+aiosqlite:///{tf.name}"
    try:
        fake = _StartNowOrch(records=[_make_record("q1", status="queued")],
                             positions={"q1": 1})
        client = _build_client(fake, db_url)
        with patch.dict(os.environ, _make_env(db_url)), client:
            resp = client.post("/api/sessions/q1/start-now", headers=_H)
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True, "status": "started"}
        assert fake.start_now_calls == ["q1"]
        rows = _audit_rows(tf.name, "session.queue_start_now")
        assert len(rows) == 1
        detail = json.loads(rows[0][1])
        assert detail["session_id"] == "q1" and "queue_reason" in detail
    finally:
        os.unlink(tf.name)


def test_start_now_account_limited_returns_409_with_earliest_reset():
    from datetime import datetime
    from viberpit.selector import AllAccountsBusy
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_url = f"sqlite+aiosqlite:///{tf.name}"
    try:
        fake = _StartNowOrch(records=[_make_record("q1", status="queued")],
                             positions={"q1": 1},
                             raises=AllAccountsBusy(datetime(2030, 1, 1, 10), "at_ceiling"))
        client = _build_client(fake, db_url)
        with patch.dict(os.environ, _make_env(db_url)), client:
            resp = client.post("/api/sessions/q1/start-now", headers=_H)
        assert resp.status_code == 409, resp.text
        body = resp.json()
        assert body["error"]["code"] == "all_accounts_limited"
        assert body["earliestReset"].startswith("2030-01-01T10:00:00")
    finally:
        os.unlink(tf.name)


def test_start_now_failed_spawn_returns_409_start_failed():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_url = f"sqlite+aiosqlite:///{tf.name}"
    try:
        fake = _StartNowOrch(records=[_make_record("q1", status="queued")],
                             positions={"q1": 1}, outcome="failed")
        client = _build_client(fake, db_url)
        with patch.dict(os.environ, _make_env(db_url)), client:
            resp = client.post("/api/sessions/q1/start-now", headers=_H)
        assert resp.status_code == 409, resp.text
        assert resp.json()["error"]["code"] == "start_failed"
    finally:
        os.unlink(tf.name)


def test_queued_create_returns_an_accurate_reason():
    """The 201 body of a queued create explains WHY, and never says capacity is free."""
    import asyncio
    from datetime import datetime, timezone

    from server.db import db_session
    from server.db.models import Session as DbSession, SessionQueue

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_url = f"sqlite+aiosqlite:///{tf.name}"
    try:
        client = _build_client(_QueueingOrch(), db_url)
        # The real orchestrator's drain would consume the seeded entry.
        with patch.dict(os.environ, _make_env(db_url)), patch.object(
                SessionOrchestrator, "_maybe_start_queued", return_value=0), client:
            async def _seed():
                async with db_session() as db:
                    db.add(DbSession(id="q-new", name="q", account_name="a", cwd="/tmp",
                                     status="queued"))
                    db.add(SessionQueue(session_id="q-new", priority=1,
                                        params={"queue_reason": "product_capacity",
                                                "repo": "org/unregistered"},
                                        enqueued_at=datetime.now(timezone.utc)))
            (client.portal.call(_seed) if getattr(client, "portal", None)
             else asyncio.run(_seed()))
            resp = client.post("/api/sessions", json={"initialPrompt": "hi"}, headers=_H)
        assert resp.status_code == 201, resp.text
        reason = resp.json()["queueStallReason"]
        assert reason["code"] == "queued"
        assert "Capacity is free" not in reason["message"]
    finally:
        os.unlink(tf.name)


@pytest.mark.parametrize("exc,code", [
    (DaemonError("UnknownMethod", "start_queued_now"), "daemon_outdated"),
    (DaemonError("RuntimeError", "boom"), "start_failed_remote"),
    (DaemonUnavailable("down"), "daemon_unavailable"),
])
def test_start_now_daemon_failures_return_503_not_500(exc, code):
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_url = f"sqlite+aiosqlite:///{tf.name}"
    try:
        fake = _StartNowOrch(records=[_make_record("q1", status="queued")],
                             positions={"q1": 1}, raises=exc)
        client = _build_client(fake, db_url)
        with patch.dict(os.environ, _make_env(db_url)), client:
            resp = client.post("/api/sessions/q1/start-now", headers=_H)
        assert resp.status_code == 503, resp.text
        assert resp.json()["error"]["code"] == code
    finally:
        os.unlink(tf.name)
