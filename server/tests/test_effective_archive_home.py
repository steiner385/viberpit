"""Tests for SessionOrchestrator._effective_archive_home() — 3-tier precedence.

Priority:
  1. DB AppSettings.archive_home (highest — runtime-configurable via UI)
  2. CCSWITCH_ARCHIVE_HOME env var  (deployment-time override)
  3. $CCSWITCH_HOME/archive symlink / default (lowest)
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from tests.helpers import temp_env


def _make_orch_with_db(db_url: str):
    from server.sessions.orchestrator import SessionOrchestrator
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    # Minimal state so _effective_archive_home can open a DB session
    orch._poll_interval = 9999.0
    return orch


def test_effective_archive_home_uses_db_value(tmp_path):
    """DB AppSettings.archive_home wins over env and default."""
    db_path = str(tmp_path / "test.db")
    db_url = f"sqlite+aiosqlite:///{db_path}"
    db_archive = tmp_path / "db_archive"

    async def _run():
        from server.db import init_db, db_session
        from server.db.models import AppSettings
        from sqlalchemy import select as sa_select

        await init_db()
        async with db_session() as db:
            row = AppSettings(id="singleton")
            row.archive_home = str(db_archive)
            db.add(row)

        orch = _make_orch_with_db(db_url)
        result = await orch._effective_archive_home()
        return result

    with temp_env():
        env_archive = str(tmp_path / "env_archive")
        with patch.dict(os.environ, {
            "CCSWITCH_DB_URL": db_url,
            "CCSWITCH_ARCHIVE_HOME": env_archive,
        }):
            result = asyncio.run(_run())

    assert result == db_archive
    assert db_archive.exists()


def test_effective_archive_home_falls_back_to_env(tmp_path):
    """When DB archive_home is null, CCSWITCH_ARCHIVE_HOME is used."""
    db_path = str(tmp_path / "test.db")
    db_url = f"sqlite+aiosqlite:///{db_path}"
    env_archive = tmp_path / "env_archive"
    env_archive.mkdir()

    async def _run():
        from server.db import init_db
        await init_db()
        orch = _make_orch_with_db(db_url)
        return await orch._effective_archive_home()

    with temp_env():
        with patch.dict(os.environ, {
            "CCSWITCH_DB_URL": db_url,
            "CCSWITCH_ARCHIVE_HOME": str(env_archive),
        }):
            result = asyncio.run(_run())

    assert result == env_archive


def test_effective_archive_home_falls_back_to_default(tmp_path):
    """When neither DB nor env is set, the default ($CCSWITCH_HOME/archive) is used."""
    db_path = str(tmp_path / "test.db")
    db_url = f"sqlite+aiosqlite:///{db_path}"

    async def _run():
        from server.db import init_db
        await init_db()
        orch = _make_orch_with_db(db_url)
        return await orch._effective_archive_home()

    with temp_env():
        os.environ.pop("CCSWITCH_ARCHIVE_HOME", None)
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": db_url}, clear=False):
            result = asyncio.run(_run())
            # Capture expected inside the same env context so CCSWITCH_HOME matches
            from ccswitch import paths
            import importlib
            importlib.reload(paths)
            expected = paths.root() / "archive"

    assert result == expected


def test_effective_archive_home_db_wins_over_env(tmp_path):
    """DB value beats env var — ensures runtime UI override takes full precedence."""
    db_path = str(tmp_path / "test.db")
    db_url = f"sqlite+aiosqlite:///{db_path}"
    db_archive = tmp_path / "db_wins"
    env_archive = tmp_path / "env_loses"

    async def _run():
        from server.db import init_db, db_session
        from server.db.models import AppSettings

        await init_db()
        async with db_session() as db:
            row = AppSettings(id="singleton")
            row.archive_home = str(db_archive)
            db.add(row)

        orch = _make_orch_with_db(db_url)
        return await orch._effective_archive_home()

    with temp_env():
        with patch.dict(os.environ, {
            "CCSWITCH_DB_URL": db_url,
            "CCSWITCH_ARCHIVE_HOME": str(env_archive),
        }):
            result = asyncio.run(_run())

    assert result == db_archive
    assert result != env_archive
