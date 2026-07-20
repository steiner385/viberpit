"""Tests for archive-home migration — PUT /api/settings {archiveHome} moves session dirs.

When the user changes `archiveHome` via the settings API the server is expected
to move every archived session directory from the old path to the new one so
that the live system stays consistent without manual intervention.
"""
from __future__ import annotations

import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from tests.helpers import temp_env

IDENTITY_HEADERS = {"Tailscale-User-Login": "testuser@example.com"}


@contextmanager
def _client_with_archives(archived_session_ids: list[str]):
    """Stand up a live TestClient with pre-seeded archived sessions on disk.

    Both the DB rows and on-disk directories are created under a single
    temp dir so the migration has real files to move.
    """
    from server.sessions.orchestrator import SessionOrchestrator
    from server.app import create_app
    from server.db import init_db, db_session
    from server.db.models import Session as DbSession
    from fastapi.testclient import TestClient
    import asyncio

    with temp_env(), tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        db_url = f"sqlite+aiosqlite:///{tmp / 'test.db'}"
        old_archive = tmp / "old_archive"
        old_archive.mkdir()

        env_patch = {
            "CCSWITCH_DB_URL": db_url,
            "CCSWITCH_HOME": str(tmp),
        }
        with patch.dict(os.environ, env_patch):
            # Seed DB rows + create on-disk dirs in old_archive
            async def _seed():
                await init_db()
                async with db_session() as db:
                    for sid in archived_session_ids:
                        db.add(DbSession(
                            id=sid,
                            name=f"session-{sid[:8]}",
                            account_name="test-account",
                            cwd=str(old_archive / sid),
                            status="archived",
                        ))
                        (old_archive / sid).mkdir(exist_ok=True)

            asyncio.run(_seed())

            # Pre-set archiveHome in the DB to old_archive so migration knows where to look
            async def _set_old():
                from server.db.models import AppSettings
                from sqlalchemy import select as sa_select
                async with db_session() as db:
                    row = (await db.execute(
                        sa_select(AppSettings).where(AppSettings.id == "singleton")
                    )).scalar_one_or_none()
                    if row is None:
                        row = AppSettings(id="singleton")
                        db.add(row)
                    row.archive_home = str(old_archive)
                    await db.flush()

            asyncio.run(_set_old())

            orch = SessionOrchestrator(poll_interval=9999.0)
            app = create_app(orchestrator=orch)
            client = TestClient(app, raise_server_exceptions=False)
            with client:
                yield client, tmp, old_archive


def test_put_archive_home_moves_session_dirs():
    """Changing archiveHome via PUT migrates all archived session dirs to the new path."""
    session_ids = ["aaaa0001-0000-0000-0000-000000000001",
                   "aaaa0002-0000-0000-0000-000000000002"]

    with _client_with_archives(session_ids) as (client, tmp, old_archive):
        new_archive = tmp / "new_archive"

        resp = client.put(
            "/api/settings",
            json={"archiveHome": str(new_archive)},
            headers=IDENTITY_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["archiveHome"] == str(new_archive)

        # Dirs must exist in the new location
        for sid in session_ids:
            assert (new_archive / sid).exists(), f"{sid} not found in new_archive"

        # Old dirs must have been moved away (not duplicated)
        for sid in session_ids:
            assert not (old_archive / sid).exists(), f"{sid} still in old_archive"


def test_put_archive_home_no_op_when_same_path():
    """Setting archiveHome to the current value does not move or duplicate dirs."""
    session_ids = ["bbbb0001-0000-0000-0000-000000000001"]

    with _client_with_archives(session_ids) as (client, tmp, old_archive):
        # Write back the SAME path — nothing should move
        resp = client.put(
            "/api/settings",
            json={"archiveHome": str(old_archive)},
            headers=IDENTITY_HEADERS,
        )
        assert resp.status_code == 200

        for sid in session_ids:
            assert (old_archive / sid).exists(), f"{sid} disappeared from old_archive"


def test_put_archive_home_clear_moves_sessions_to_default():
    """PUT {archiveHome: ''} moves sessions to the default archive path ($CCSWITCH_HOME/archive)."""
    session_ids = ["cccc0001-0000-0000-0000-000000000001"]

    with _client_with_archives(session_ids) as (client, tmp, old_archive):
        default_archive = tmp / "archive"

        resp = client.put(
            "/api/settings",
            json={"archiveHome": ""},
            headers=IDENTITY_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["archiveHome"] is None

        # Sessions must be in the default archive dir now
        for sid in session_ids:
            assert (default_archive / sid).exists(), f"{sid} not in default archive"
