"""Tests for Task 6: API surfaces real status/createdAt for rehydrated sessions."""
from __future__ import annotations

import asyncio
import os
from datetime import datetime
from unittest.mock import patch

from fastapi.testclient import TestClient

from server.app import create_app
from server.db import init_db, db_session
from server.db.models import Session as DbSession
from ccswitch.selector import Picked
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory

_IDENT = {"Tailscale-User-Login": "apitest@example.com"}


def test_rehydrated_session_reports_real_status_and_created_at(tmp_path):
    """GET /api/sessions returns real status and createdAt from the DB row."""
    db_path = tmp_path / "ccswitch.db"
    db_url = f"sqlite+aiosqlite:///{db_path}"
    session_id = "rehydrate-api-s1"
    cwd = str(tmp_path)
    created_at = datetime(2026, 6, 20, 12, 0, 0)

    async def _seed():
        await init_db(db_url=db_url)
        async with db_session() as db:
            row = DbSession(
                id=session_id,
                name="rehydrated-api-session",
                account_name="acct1",
                engine_session_id=None,
                status="idle",
                cwd=cwd,
                created_at=created_at,
            )
            db.add(row)

    asyncio.run(_seed())

    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
    app = create_app(orchestrator=orch)

    with patch.dict(os.environ, {"VIBERPIT_DB_URL": db_url}), \
         TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/api/sessions", headers=_IDENT)
        assert resp.status_code == 200, resp.text
        sessions = resp.json()
        sess = next((s for s in sessions if s["id"] == session_id), None)
        assert sess is not None, f"session {session_id!r} not found in {sessions}"
        assert sess["status"] == "idle", f"expected status=idle, got {sess['status']!r}"
        assert sess["createdAt"] == "2026-06-20T12:00:00+00:00", (
            f"expected createdAt=2026-06-20T12:00:00+00:00, got {sess['createdAt']!r}"
        )


def test_create_session_status_reflects_initial_prompt(tmp_path):
    """POST /api/sessions: a session WITH an initialPrompt has already been handed
    that prompt when the create call returns — it starts working without anyone
    opening it (the deferred-until-attach design of #4 is gone). A session WITHOUT
    one has nothing to run and is 'idle'.
    """
    import time as _time
    from ccswitch import paths
    from ccswitch.creds import write_json_secure

    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"
    store_home = tmp_path / "store"
    store_home.mkdir()
    cfg_dir = tmp_path / "cfg" / "alpha"
    cfg_dir.mkdir(parents=True)
    shared_base = tmp_path / "dot-claude"
    shared_base.mkdir()
    (shared_base / "settings.json").write_text("{}")
    (shared_base / "projects").mkdir()

    def _fake_usage(account):
        from datetime import timedelta, timezone
        from ccswitch.quota import anthropic_snapshot
        reset = datetime.now(timezone.utc) + timedelta(hours=5)
        return anthropic_snapshot(10.0, reset, 10.0, reset, False)

    env = {
        "VIBERPIT_HOME": str(store_home),
        "CLAUDE_CONFIG_DIR": str(shared_base),
        "VIBERPIT_DB_URL": db_url,
    }
    with patch.dict(os.environ, env), \
         patch("server.sessions.orchestrator.compose", return_value=cfg_dir), \
         patch("server.sessions.orchestrator.configdir"), \
         patch("server.sessions.orchestrator.select_account", return_value=Picked("alpha")), \
         patch("ccswitch.accounts.usage_for", side_effect=_fake_usage):

        # Seed a fake account credential — MUST be inside the VIBERPIT_HOME patch,
        # otherwise paths.slot_path() resolves to the real store and leaks an
        # "alpha" account into the user's ~/.claude-accounts.
        now_ms = int(_time.time() * 1000)
        write_json_secure(paths.slot_path("alpha"), {
            "accessToken": "alpha-tok",
            "refreshToken": "alpha-ref",
            "expiresAt": now_ms + 9_000_000,
            "subscriptionType": "max",
        })
        shared_mcp = paths.shared_mcp_path()
        if not shared_mcp.exists():
            write_json_secure(shared_mcp, {})

        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        app = create_app(orchestrator=orch)

        with TestClient(app, raise_server_exceptions=False) as client:
            # Session WITH initial_prompt → the prompt has reached the SDK by the time
            # the create call returns; nothing waits for a WS attach. (The fake's turn
            # may or may not have settled yet, so the status itself is not pinned.)
            resp_with = client.post(
                "/api/sessions",
                json={"name": "With prompt", "initialPrompt": "hello there"},
                headers=_IDENT,
            )
            assert resp_with.status_code == 201, resp_with.text
            data_with = resp_with.json()
            assert orch._drivers[data_with["id"]]._client.sent == ["hello there"], (
                "the initial prompt was not sent at creation — the session sits at "
                "'Ready' until someone navigates into it"
            )
            assert orch.pop_pending_initial_prompt(data_with["id"]) is None

            # Session WITHOUT initial_prompt → status should also be "idle"
            resp_without = client.post(
                "/api/sessions",
                json={"name": "No prompt"},
                headers=_IDENT,
            )
            assert resp_without.status_code == 201, resp_without.text
            data_without = resp_without.json()
            assert data_without["status"] == "idle", (
                f"expected status=idle without initialPrompt, got {data_without['status']!r}"
            )
