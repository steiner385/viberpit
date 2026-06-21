"""REST endpoints: GET /api/sessions/adoptable + POST /api/sessions/adopt."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient

from server.sessions.adopt import AdoptionCandidate

H = {"Tailscale-User-Login": "testuser@example.com"}


def _client():
    from server.app import create_app
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory

    with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:",
                                 "CCSWITCH_DEV": "1", "CCSWITCH_TAILNET_ALLOWLIST": ""}):
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        app = create_app(orchestrator=orch)
        return TestClient(app, raise_server_exceptions=False), orch


def _cand(sid, **kw):
    base = dict(pid=5, account="kindash", cwd="/home/u/repo", claude_session_id=sid,
                transcript_path="/x.jsonl", name="Work", mapped=True, tracked=False,
                skip_reason=None, last_activity=datetime.now(timezone.utc), pid_alive=True)
    base.update(kw)
    return AdoptionCandidate(**base)


def test_adoptable_requires_identity():
    client, _ = _client()
    with patch("server.api.sessions._adopt.discover_candidates", return_value=[]):
        assert client.get("/api/sessions/adoptable").status_code == 401


def test_adoptable_returns_flagged_candidates():
    client, _ = _client()
    cands = [_cand("aaa"), _cand("bbb", mapped=False, account=None, skip_reason="unmapped")]
    with patch("server.api.sessions._adopt.discover_candidates", return_value=cands):
        r = client.get("/api/sessions/adoptable", headers=H)
    assert r.status_code == 200
    body = r.json()["candidates"]
    assert body[0]["claudeSessionId"] == "aaa" and body[0]["skipReason"] is None
    assert body[1]["skipReason"] == "unmapped" and "lastActivity" in body[1]
    assert body[0]["pidAlive"] is True


def test_adopt_happy_path(monkeypatch):
    client, orch = _client()
    from server.sessions.types import SessionRecord
    rec = SessionRecord(id="newid", name="Work", account="kindash", claude_session_id="aaa",
                        cols=80, rows=24, cwd="/home/u/repo", adopted_in_place=True)

    async def fake_adopt(sid, identity, **kw):
        return rec, {"resumed": True, "terminatedPid": 5}

    monkeypatch.setattr(orch, "adopt_session", fake_adopt)
    r = client.post("/api/sessions/adopt", json={"claudeSessionId": "aaa"}, headers=H)
    assert r.status_code == 201
    body = r.json()
    assert body["resumed"] is True and body["terminatedPid"] == 5
    assert body["session"]["adoptedInPlace"] is True


def test_adopt_conflict_when_not_adoptable(monkeypatch):
    client, orch = _client()

    async def fake_adopt(sid, identity, **kw):
        raise KeyError(sid)

    monkeypatch.setattr(orch, "adopt_session", fake_adopt)
    r = client.post("/api/sessions/adopt", json={"claudeSessionId": "gone"}, headers=H)
    assert r.status_code == 409


def test_adopt_requires_identity():
    client, _ = _client()
    assert client.post("/api/sessions/adopt", json={"claudeSessionId": "x"}).status_code == 401
