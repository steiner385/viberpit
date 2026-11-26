"""provenance — the session_events timeline recorder and its endpoint (#947)."""
from __future__ import annotations

import asyncio
import logging
from unittest.mock import patch

from fastapi.testclient import TestClient

from server.app import create_app
from server.sessions import provenance
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory
from server.tests.test_api import IDENTITY_HEADERS


def test_record_then_list_newest_first(isolated_db):
    async def run():
        await provenance.record("s1", provenance.SPAWN_FAILED, {"reason": "network"})
        await provenance.record("s1", provenance.AUTO_ARCHIVED, {"by": "completion"})
        await provenance.record("other", provenance.AUTO_ARCHIVED, None)
        return await provenance.list_events("s1")

    events = asyncio.run(run())
    assert [e["kind"] for e in events] == [provenance.AUTO_ARCHIVED, provenance.SPAWN_FAILED]
    assert events[1]["detail"] == {"reason": "network"}
    assert all(isinstance(e["ts"], str) and e["ts"] for e in events)


def test_list_respects_limit_and_null_detail(isolated_db):
    async def run():
        for _ in range(3):
            await provenance.record("s2", provenance.AUTO_ARCHIVED)
        return await provenance.list_events("s2", limit=2)

    events = asyncio.run(run())
    assert len(events) == 2
    assert events[0]["detail"] == {}


def test_record_swallows_db_failure(isolated_db, caplog):
    """Provenance never breaks the caller: a failed write is a WARNING, not a raise."""
    def boom():
        raise RuntimeError("db down")

    with patch("server.sessions.provenance.db_session", boom), \
            caplog.at_level(logging.WARNING, logger="server.sessions.provenance"):
        asyncio.run(provenance.record("s3", provenance.SPAWN_FAILED, {}))
    assert any("provenance.record_failed" in r.getMessage() for r in caplog.records)


def test_unknown_kind_is_refused_not_written(isolated_db, caplog):
    with caplog.at_level(logging.ERROR, logger="server.sessions.provenance"):
        asyncio.run(provenance.record("s4", "made_up"))
    assert asyncio.run(provenance.list_events("s4")) == []
    assert any("provenance.unknown_kind" in r.getMessage() for r in caplog.records)


def test_events_endpoint(isolated_db):
    asyncio.run(provenance.record("s5", provenance.SPAWN_FAILED, {"reason": "auth"}))
    app = create_app(orchestrator=SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0))
    with TestClient(app) as client:
        r = client.get("/api/sessions/s5/events", headers=IDENTITY_HEADERS)
        assert r.status_code == 200
        assert [e["kind"] for e in r.json()["events"]] == [provenance.SPAWN_FAILED]
        # The timeline outlives the row: an unknown id is empty, not a 404.
        assert client.get("/api/sessions/nope/events",
                          headers=IDENTITY_HEADERS).json() == {"events": []}
        assert client.get("/api/sessions/s5/events?limit=0",
                          headers=IDENTITY_HEADERS).status_code == 422
