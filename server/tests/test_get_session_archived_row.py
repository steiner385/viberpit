"""GET /api/sessions/{id} answers for an archived/ended row, not 404 (#3082).

The orchestrator's ``get_session`` only knows live records, so an archived session
404'd while its transcript and export (which fall back to the DB row) still served.
The assistant's ``get_session`` tool read that 404 as "the session vanished" after a
deploy, for four sessions that had simply finished and auto-archived on completion.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime
from unittest.mock import patch

from fastapi.testclient import TestClient

from server.app import create_app
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory

_IDENT = {"Tailscale-User-Login": "apitest@example.com"}


def _seed(db_url: str, **row) -> None:
    from server.db import db_session, init_db
    from server.db.models import Session as DbSession

    async def run():
        await init_db(db_url=db_url)
        async with db_session() as db:
            db.add(DbSession(**row))

    asyncio.run(run())


def _get(tmp_path, sid: str):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"
    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0)
    app = create_app(orchestrator=orch)
    with patch.dict(os.environ, {"VIBERPIT_DB_URL": db_url}), \
         TestClient(app, raise_server_exceptions=False) as client:
        return client.get(f"/api/sessions/{sid}", headers=_IDENT)


def test_archived_row_is_served_with_its_real_status(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"
    archived_at = datetime(2026, 10, 14, 12, 9, 7)
    with patch.dict(os.environ, {"VIBERPIT_DB_URL": db_url}):
        _seed(db_url, id="arch-1", name="FCM rollout", account_name="acct",
              engine_session_id="eng-1", status="archived", cwd=str(tmp_path),
              host="rocky", archived_by="completion", archived_at=archived_at)
    r = _get(tmp_path, "arch-1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == "arch-1"
    assert body["status"] == "archived"
    assert body["name"] == "FCM rollout"
    assert body["host"] == "rocky"
    assert body["archivedBy"] == "completion"
    assert body["archivedAt"].startswith("2026-10-14T12:09:07")
    assert body["detached"] is True


def test_unknown_id_still_404s(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"
    with patch.dict(os.environ, {"VIBERPIT_DB_URL": db_url}):
        _seed(db_url, id="other", name="x", account_name="acct",
              engine_session_id=None, status="archived", cwd=str(tmp_path))
    r = _get(tmp_path, "nope")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "not_found"
