"""POST /api/sessions/batch/rotate, /{id}/rotate, /{id}/account-pin."""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from server.app import create_app
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.types import SessionRecord
from server.tests.fakes.fake_factory import fake_sdk_factory

_H = {"Tailscale-User-Login": "testuser@example.com"}
_ENV = {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:", "CCSWITCH_DEV": "1",
        "CCSWITCH_TAILNET_ALLOWLIST": ""}


def _rec(sid="s1", **kw):
    base = dict(id=sid, name="n", account="a", claude_session_id="c", cols=80, rows=24,
                cwd="/tmp", status="needs_attention")
    base.update(kw)
    return SessionRecord(**base)


class _FakeSessions:
    def __init__(self):
        self.rotate_map: dict[str, str] = {}
        self.records: dict[str, SessionRecord] = {"s1": _rec()}
        self.calls: list[tuple] = []

    async def rotate_to_best(self, sid, identity="system"):
        self.calls.append(("rotate", sid, identity))
        return self.rotate_map.get(sid, "skipped:not-found")

    async def set_account_pin(self, sid, pinned):
        self.calls.append(("pin", sid, pinned))
        rec = self.records.get(sid)
        if rec is None:
            return "skipped:not-found"
        rec.account_pinned = pinned
        return "pinned" if pinned else "unpinned"

    async def get_session(self, sid):
        return self.records.get(sid)

    async def prs_for(self, sid): return []
    async def latest_suggestions(self, sid): return []
    async def latest_summary(self, sid): return ""


def _make_client():
    with patch.dict(os.environ, _ENV):
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0,
                                   usage_fn=lambda name: None)
        app = create_app(orchestrator=orch)
    fake = _FakeSessions()
    app.state.sessions = fake
    return TestClient(app, raise_server_exceptions=False), fake


@pytest.fixture
def cf():
    client, fake = _make_client()
    with patch.dict(os.environ, _ENV), client:
        yield client, fake


def test_batch_rotate_partitions_by_outcome(cf):
    client, fake = cf
    fake.rotate_map = {"s1": "rotated", "s2": "skipped:all_accounts_busy"}
    body = client.post("/api/sessions/batch/rotate", json={"ids": ["s1", "s2", "s1"]}, headers=_H).json()
    assert body["succeeded"] == ["s1"]
    assert body["failed"] == [{"id": "s2", "error": "skipped:all_accounts_busy"}]
    assert fake.calls.count(("rotate", "s1", "testuser@example.com")) == 1


def test_batch_rotate_empty_ids_422(cf):
    client, _ = cf
    assert client.post("/api/sessions/batch/rotate", json={"ids": []}, headers=_H).status_code == 422


def test_rotate_one_returns_record(cf):
    client, fake = cf
    fake.rotate_map = {"s1": "rotated"}
    r = client.post("/api/sessions/s1/rotate", headers=_H)
    assert r.status_code == 200, r.text
    assert r.json()["id"] == "s1"


def test_rotate_one_409_on_skip(cf):
    client, fake = cf
    fake.rotate_map = {"s1": "skipped:all_accounts_busy"}
    r = client.post("/api/sessions/s1/rotate", headers=_H)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "skipped:all_accounts_busy"


def test_rotate_one_404_unknown(cf):
    client, _ = cf
    assert client.post("/api/sessions/zz/rotate", headers=_H).status_code == 404


def test_account_pin_unpins(cf):
    client, fake = cf
    fake.records["s1"].account_pinned = True
    r = client.post("/api/sessions/s1/account-pin", json={"pinned": False}, headers=_H)
    assert r.status_code == 200, r.text
    assert r.json()["accountPinned"] is False
    assert ("pin", "s1", False) in fake.calls


def test_account_pin_404_unknown(cf):
    client, _ = cf
    assert client.post("/api/sessions/zz/account-pin", json={"pinned": True}, headers=_H).status_code == 404


def test_routes_require_identity():
    client, _ = _make_client()
    with patch.dict(os.environ, _ENV), client:
        assert client.post("/api/sessions/batch/rotate", json={"ids": ["s1"]}).status_code == 401
        assert client.post("/api/sessions/s1/rotate").status_code == 401
