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
from pathlib import Path

import pytest

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


@pytest.mark.parametrize("repo_root", [None, "/home/tony/GitHub", "/srv/repos", "/tmp-live/repos"])
def test_guard_isolates_repo_root_even_when_store_and_db_are_isolated(
    tmp_path, monkeypatch, repo_root,
):
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path / "store"))
    monkeypatch.setenv("CCSWITCH_DB_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.delenv("CCSWITCH_ARCHIVE_HOME", raising=False)
    monkeypatch.delenv("VIBERPIT_REPO_ROOT", raising=False)
    if repo_root is None:
        monkeypatch.delenv("CCSWITCH_REPO_ROOT", raising=False)
    else:
        monkeypatch.setenv("CCSWITCH_REPO_ROOT", repo_root)

    _redirect_state_off_production()

    from server.sessions.orchestrator import SessionOrchestrator

    root = Path(SessionOrchestrator()._repo_root)
    assert root.is_relative_to(Path(tempfile.gettempdir()).resolve())
    assert root.is_dir()
    assert str(root) != repo_root


def test_guard_preserves_explicit_temporary_repo_root(tmp_path, monkeypatch):
    monkeypatch.setenv("CCSWITCH_REPO_ROOT", str(tmp_path))
    _redirect_state_off_production()
    assert os.environ["CCSWITCH_REPO_ROOT"] == str(tmp_path)


def test_guard_preserves_new_prefix_temporary_repo_root(tmp_path, monkeypatch):
    monkeypatch.delenv("CCSWITCH_REPO_ROOT", raising=False)
    monkeypatch.setenv("VIBERPIT_REPO_ROOT", str(tmp_path))
    _redirect_state_off_production()
    assert os.environ["CCSWITCH_REPO_ROOT"] == str(tmp_path)
    assert os.environ["VIBERPIT_REPO_ROOT"] == str(tmp_path)


def test_guard_fences_unsafe_new_prefix_with_safe_legacy_root(tmp_path, monkeypatch):
    monkeypatch.setenv("CCSWITCH_REPO_ROOT", str(tmp_path))
    monkeypatch.setenv("VIBERPIT_REPO_ROOT", "/srv/live-repos")
    _redirect_state_off_production()
    assert os.environ["CCSWITCH_REPO_ROOT"] == str(tmp_path)
    assert os.environ["VIBERPIT_REPO_ROOT"] == str(tmp_path)
