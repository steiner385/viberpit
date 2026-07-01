"""App startup upserts automation rows (pr-doctor present after boot, via direct DB read)."""
from __future__ import annotations

import asyncio
import os
from unittest.mock import patch

H = {"Tailscale-User-Login": "testuser@example.com"}


def test_startup_creates_pr_doctor_row():
    """ensure_rows() fires in lifespan → row exists in DB before any HTTP request."""
    from fastapi.testclient import TestClient
    from server.app import create_app
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory
    from sqlalchemy import select as _sa_select

    with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:",
                                 "CCSWITCH_DEV": "1", "CCSWITCH_TAILNET_ALLOWLIST": ""}):
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        app = create_app(orchestrator=orch)
        with TestClient(app) as _client:  # context-manager triggers lifespan startup
            # Direct DB read — NOT an HTTP request. The router's lazy _row create-on-read
            # would pass even without ensure_rows(); a direct query proves startup wiring.
            from server.db import db_session
            from server.db.models import Automation

            async def _fetch():
                async with db_session() as db:
                    return (
                        await db.execute(
                            _sa_select(Automation).where(Automation.id == "pr-doctor")
                        )
                    ).scalar_one_or_none()

            row = asyncio.run(_fetch())
            assert row is not None, "pr-doctor row must exist after lifespan startup"
            assert row.enabled is False, "default enabled state must be False"
