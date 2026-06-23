"""REST endpoint: GET /api/sessions/search?q=..."""
from __future__ import annotations

import os
from unittest.mock import patch

from fastapi.testclient import TestClient

from server.sessions.types import SessionRecord

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


def _rec(sid, name):
    return SessionRecord(id=sid, name=name, account="kindash", claude_session_id=sid,
                         cols=80, rows=24, cwd="/home/u/repo")


def _setup(orch, tmp_path, monkeypatch, records, texts):
    # texts: {session_id: transcript text or None for "no transcript"}
    paths = {}
    for sid, txt in texts.items():
        if txt is None:
            paths[sid] = None
            continue
        p = tmp_path / f"{sid}.jsonl"
        p.write_text('{"type":"user","message":{"role":"user","content":%s}}\n' % _json(txt))
        paths[sid] = str(p)
    monkeypatch.setattr(orch, "list_sessions", lambda: list(records))
    monkeypatch.setattr(orch, "transcript_path", lambda sid: paths.get(sid))


def _json(s):
    import json
    return json.dumps(s)


def test_search_requires_identity():
    client, _ = _client()
    assert client.get("/api/sessions/search?q=hi").status_code == 401


def test_search_matches_name_and_transcript(tmp_path, monkeypatch):
    client, orch = _client()
    records = [_rec("s1", "Auth work"), _rec("s2", "Billing"), _rec("s3", "Docs")]
    _setup(orch, tmp_path, monkeypatch, records, {
        "s1": "nothing relevant here",
        "s2": "we fixed the auth token refresh",
        "s3": None,  # no transcript → name-only
    })
    # 'auth' matches s1 (name) and s2 (transcript), not s3.
    r = client.get("/api/sessions/search?q=auth", headers=H)
    assert r.status_code == 200
    assert sorted(r.json()["ids"]) == ["s1", "s2"]


def test_search_is_and_of_terms(tmp_path, monkeypatch):
    client, orch = _client()
    records = [_rec("s1", "Auth"), _rec("s2", "Auth bug")]
    _setup(orch, tmp_path, monkeypatch, records, {
        "s1": "token refresh",          # has 'auth' (name) but not 'bug'
        "s2": "the bug is fixed",        # has both 'auth' (name) and 'bug'
    })
    r = client.get("/api/sessions/search?q=auth+bug", headers=H)
    assert r.json()["ids"] == ["s2"]


def test_search_blank_query_returns_all_active(tmp_path, monkeypatch):
    client, orch = _client()
    records = [_rec("s1", "A"), _rec("s2", "B")]
    _setup(orch, tmp_path, monkeypatch, records, {"s1": "x", "s2": "y"})
    r = client.get("/api/sessions/search?q=", headers=H)
    assert sorted(r.json()["ids"]) == ["s1", "s2"]
