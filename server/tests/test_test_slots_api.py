"""HTTP contract for /api/test-slots/{acquire,release,status} (#822).

Bare-FastAPI + init_db pattern (mirrors test_client_logs_api.py), since this
router owns its own DB access (via server.sessions.test_run_slots) rather
than taking a get_db dependency, and needs no orchestrator.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CCSWITCH_DEV", "1")
    from server.db import init_db
    asyncio.run(init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'api.db'}"))

    from server.api.test_slots import router
    from server.auth.middleware import current_identity

    app = FastAPI()
    app.dependency_overrides[current_identity] = lambda: "tester@example.com"
    app.include_router(router)
    return TestClient(app)


def test_acquire_grants_when_under_cap(client, monkeypatch):
    monkeypatch.setenv("CCSWITCH_TEST_SLOT_CAP", "1")
    r = client.post("/api/test-slots/acquire", json={"sessionId": "s1", "host": "rocky"})
    assert r.status_code == 200
    body = r.json()
    assert body["granted"] is True
    assert body["ahead"] == 0
    assert body["cap"] == 1
    assert body["degraded"] is False


def test_acquire_queues_over_cap(client, monkeypatch):
    monkeypatch.setenv("CCSWITCH_TEST_SLOT_CAP", "1")
    client.post("/api/test-slots/acquire", json={"sessionId": "s1", "host": "rocky"})
    r = client.post("/api/test-slots/acquire", json={"sessionId": "s2", "host": "rocky"})
    body = r.json()
    assert body["granted"] is False
    assert body["ahead"] == 0


def test_status_reflects_waiting_position(client, monkeypatch):
    monkeypatch.setenv("CCSWITCH_TEST_SLOT_CAP", "1")
    client.post("/api/test-slots/acquire", json={"sessionId": "s1", "host": "rocky"})
    client.post("/api/test-slots/acquire", json={"sessionId": "s2", "host": "rocky"})
    r = client.get("/api/test-slots/status", params={"sessionId": "s2", "host": "rocky"})
    assert r.status_code == 200
    assert r.json() == {"status": "waiting", "ahead": 0, "cap": 1, "degraded": False}


def test_release_promotes_next_waiter(client, monkeypatch):
    monkeypatch.setenv("CCSWITCH_TEST_SLOT_CAP", "1")
    client.post("/api/test-slots/acquire", json={"sessionId": "s1", "host": "rocky"})
    client.post("/api/test-slots/acquire", json={"sessionId": "s2", "host": "rocky"})

    r = client.post("/api/test-slots/release", json={"sessionId": "s1"})
    assert r.status_code == 200
    assert r.json() == {"released": True}

    r2 = client.get("/api/test-slots/status", params={"sessionId": "s2", "host": "rocky"})
    assert r2.json()["status"] == "holding"


def test_acquire_fails_open_on_internal_error(client, monkeypatch):
    async def boom(*a, **kw):
        raise RuntimeError("db unreachable")
    import server.api.test_slots as mod
    monkeypatch.setattr(mod.test_run_slots, "acquire", boom)

    r = client.post("/api/test-slots/acquire", json={"sessionId": "s1", "host": "rocky"})
    assert r.status_code == 200
    body = r.json()
    assert body["granted"] is True
    assert body["degraded"] is True


def test_status_fails_open_on_internal_error(client, monkeypatch):
    async def boom(*a, **kw):
        raise RuntimeError("db unreachable")
    import server.api.test_slots as mod
    monkeypatch.setattr(mod.test_run_slots, "status", boom)

    r = client.get("/api/test-slots/status", params={"sessionId": "s1", "host": "rocky"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "unknown"
    assert body["degraded"] is True


def test_release_never_raises_on_internal_error(client, monkeypatch):
    async def boom(*a, **kw):
        raise RuntimeError("db unreachable")
    import server.api.test_slots as mod
    monkeypatch.setattr(mod.test_run_slots, "release", boom)

    r = client.post("/api/test-slots/release", json={"sessionId": "s1"})
    assert r.status_code == 200
    assert r.json() == {"released": True}


def test_requires_auth():
    from server.api.test_slots import router
    app = FastAPI()
    app.include_router(router)  # no dependency override → real auth
    r = TestClient(app).post("/api/test-slots/acquire", json={"sessionId": "s1", "host": "rocky"})
    assert r.status_code in (401, 403)
