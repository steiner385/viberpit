"""Regression tests for the conftest production-isolation guard.

The guard in ``conftest._redirect_state_off_production`` must recognise the
PRODUCTION database so an inherited ``CCSWITCH_DB_URL`` can never point the
suite at it. Its original detection (``str(_REAL_HOME) in db``) only matched
the legacy SQLite store at ``~/.claude-accounts/ccswitch.db``; after production
migrated to Postgres, a URL like ``postgresql://…/ccswitch`` was silently
classified as non-production and the whole suite ran against live Postgres —
fencing the primary lease and churning the live ``sessions`` table.
"""
import os
import tempfile

from server.tests.conftest import (
    _REAL_HOME,
    _archive_home_is_production,
    _db_is_production,
    _redirect_state_off_production,
)


def test_postgres_production_url_is_detected_as_production():
    # The bug: a networked prod DB whose name is not a 'test' DB.
    assert _db_is_production(
        "postgresql+asyncpg://ccswitch:pw@127.0.0.1:5432/ccswitch") is True


def test_postgres_test_database_is_not_production():
    # Matches the pg_url naming convention: db name contains 'test'.
    assert _db_is_production(
        "postgresql+asyncpg://ccswitch:pw@127.0.0.1:5432/ccswitch_test") is False


def test_unset_url_is_treated_as_production():
    # Unset → server.config.Settings defaults to the real store.
    assert _db_is_production(None) is True


def test_in_memory_sqlite_is_not_production():
    assert _db_is_production("sqlite+aiosqlite:///:memory:") is False


def test_temp_file_sqlite_is_not_production(tmp_path):
    assert _db_is_production(
        f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}") is False


def test_legacy_real_home_sqlite_is_production():
    assert _db_is_production(
        f"sqlite+aiosqlite:///{_REAL_HOME}/ccswitch.db") is True


def test_ambient_archive_home_is_production():
    # A deployment archive path (outside the system temp dir) is production.
    assert _archive_home_is_production("/mnt/hdd-storage/ccswitch-archives") is True


def test_unset_archive_home_is_not_production():
    # Unset → archive_dir() falls back to $CCSWITCH_HOME/archive (isolated).
    assert _archive_home_is_production(None) is False


def test_temp_archive_home_is_not_production():
    # A test's own tmp_path archive lives under the temp dir — respect it.
    p = f"{tempfile.gettempdir()}/pytest-of-tony/archive"
    assert _archive_home_is_production(p) is False


def test_guard_unsets_inherited_archive_home(monkeypatch):
    """End-to-end: an inherited production CCSWITCH_ARCHIVE_HOME must be unset
    so archive_dir() falls back to each test's own $CCSWITCH_HOME/archive."""
    monkeypatch.setenv("CCSWITCH_ARCHIVE_HOME", "/mnt/hdd-storage/ccswitch-archives")
    monkeypatch.setenv("CCSWITCH_HOME", str(_REAL_HOME))

    _redirect_state_off_production()

    assert "CCSWITCH_ARCHIVE_HOME" not in os.environ


def test_guard_redirects_inherited_postgres_prod_url(monkeypatch):
    """End-to-end: an inherited prod Postgres CCSWITCH_DB_URL must be rewritten
    to a throwaway SQLite DB, never left pointing at production."""
    prod = "postgresql+asyncpg://ccswitch:pw@127.0.0.1:5432/ccswitch"
    monkeypatch.setenv("CCSWITCH_DB_URL", prod)
    monkeypatch.setenv("CCSWITCH_HOME", str(_REAL_HOME))

    _redirect_state_off_production()

    result = os.environ["CCSWITCH_DB_URL"]
    assert result != prod
    assert result.startswith("sqlite")
