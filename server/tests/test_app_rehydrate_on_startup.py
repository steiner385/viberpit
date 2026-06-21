"""Tests for Task 3: app startup rehydrates persisted sessions via orchestrator.rehydrate()."""
import asyncio
import os
from unittest.mock import patch

from fastapi.testclient import TestClient

from server.app import create_app
from server.db import init_db, db_session
from server.db.models import Session as DbSession
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory

_IDENT = {"Tailscale-User-Login": "rehydrate@example.com"}


def test_startup_rehydrates_sessions_into_list(tmp_path):
    db_path = tmp_path / "ccswitch.db"
    db_url = f"sqlite+aiosqlite:///{db_path}"

    async def seed():
        await init_db(db_url=db_url)
        async with db_session() as db:
            db.add(DbSession(id="restored", name="Restored", account_name="acct",
                             claude_session_id="claude-x", status="working",
                             cwd=str(tmp_path)))
    asyncio.run(seed())

    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
    app = create_app(orchestrator=orch)
    with patch.dict(os.environ, {"CCSWITCH_DB_URL": db_url}), \
         TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/api/sessions", headers=_IDENT)
        assert resp.status_code == 200, resp.text
        ids = [s["id"] for s in resp.json()]
        assert "restored" in ids
