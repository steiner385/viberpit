"""POST /api/internal/automation-sessions: the primary daemon's own loopback route
for automation-originated session creation (spec: automation dispatch balance,
2026-09-20). Auth gating (loopback-only, exact-route, expired/garbage token) plus
the success/refusal envelope contract that server/sessions/internal_dispatch.py's
client depends on.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient

from server.sessions.selection import AllAccountsBusy
from server.app import create_app
from server.auth.internal_token import mint_token
from server.sessions.engine import AgentEngine
from server.sessions.types import SessionRecord

_ENV = {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:", "CCSWITCH_DEV": "1",
        "CCSWITCH_TAILNET_ALLOWLIST": "", "CCSWITCH_SESSION_SECRET": "x" * 32}


def _client() -> TestClient:
    with patch.dict(os.environ, _ENV):
        app = create_app()
    return TestClient(app, client=("127.0.0.1", 5000))


def _far_client(app) -> TestClient:
    return TestClient(app, client=("203.0.113.9", 5000))


def _record(**over) -> SessionRecord:
    fields = dict(
        id="s1", name="x", account="acct", engine=AgentEngine.CLAUDE_CODE,
        engine_session_id="e1", cols=80, rows=24, cwd="/tmp",
        status="running", host=None,
        created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
    )
    fields.update(over)
    return SessionRecord(**fields)


class _FakeOrch:
    def __init__(self, *, rec=None, exc=None):
        self._rec = rec
        self._exc = exc
        self.calls: list[dict] = []

    async def create_session(self, **kwargs):
        self.calls.append(kwargs)
        if self._exc is not None:
            raise self._exc
        return self._rec


def test_non_loopback_peer_refused():
    with patch.dict(os.environ, _ENV):
        tok = mint_token()
        c = _client()
        far = _far_client(c.app)
        r = far.post("/api/internal/automation-sessions",
                     json={"name": "x"}, headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 401


def test_garbage_or_expired_token_refused():
    with patch.dict(os.environ, _ENV):
        c = _client()
        r = c.post("/api/internal/automation-sessions",
                   json={"name": "x"}, headers={"Authorization": "Bearer ccsys1:nope"})
        assert r.status_code == 401


def test_token_cannot_reach_any_other_route():
    with patch.dict(os.environ, _ENV):
        tok = mint_token()
        c = _client()
        r = c.get("/api/sessions", headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "INTERNAL_ROUTE_FORBIDDEN"


def test_happy_path_forwards_kwargs_and_returns_session():
    with patch.dict(os.environ, _ENV):
        tok = mint_token()
        c = _client()
        fake = _FakeOrch(rec=_record())
        c.app.state.sessions = fake
        r = c.post("/api/internal/automation-sessions",
                   json={"name": "backlog x", "origin": "backlog-runner",
                         "origin_ref": "owner/repo#1"},
                   headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 201, r.text
        assert r.json()["session"]["id"] == "s1"
        # Fields the caller never set are OMITTED, not sent as null — so the real
        # orchestrator's own defaults (e.g. permission_mode="interactive") apply
        # exactly as they would for a direct in-process call. `prefer` is always
        # forwarded explicitly (None when unset), matching POST /api/sessions'
        # existing `prefer=getattr(body, "host", None)` — both FleetOrchestrator
        # and InProcessOrchestratorAdapter (server/sessions/facade.py) accept it,
        # the latter by popping it before the real single-host create_session call.
        assert fake.calls == [{"prefer": None, "name": "backlog x",
                               "origin": "backlog-runner", "origin_ref": "owner/repo#1"}]


def test_domain_refusal_returns_422_envelope_the_client_can_reconstruct():
    with patch.dict(os.environ, _ENV):
        tok = mint_token()
        c = _client()
        reset = datetime(2026, 7, 8, 21, 0, tzinfo=timezone.utc)
        c.app.state.sessions = _FakeOrch(exc=AllAccountsBusy(reset, kind="at_ceiling"))
        r = c.post("/api/internal/automation-sessions",
                   json={"name": "x"}, headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 422
        err = r.json()["error"]
        assert err["type"] == "AllAccountsBusy"
        assert err["data"] == {"earliest_reset": reset.isoformat(), "kind": "at_ceiling"}


def test_unknown_field_rejected():
    with patch.dict(os.environ, _ENV):
        tok = mint_token()
        c = _client()
        c.app.state.sessions = _FakeOrch(rec=_record())
        r = c.post("/api/internal/automation-sessions",
                   json={"name": "x", "totally_unexpected_field": 1},
                   headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 422
