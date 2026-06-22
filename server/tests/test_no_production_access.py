"""Structural guard: the test session must never resolve to the REAL store.

Regression lock-in for the recurring production-state leak — tests without
``temp_env()`` read/wrote the live ``~/.claude-accounts`` ledger and DB,
spuriously stamping ``needs-reauth`` onto the real ``primary`` account and
leaving phantom ``alpha`` / ``dave`` accounts behind.

``conftest.py`` redirects ``CCSWITCH_HOME`` and ``CCSWITCH_DB_URL`` to a
per-session temp dir at import time, BEFORE any path/DB resolution happens.
These tests assert that redirection is in force: if someone deletes it (or a
future ``conftest`` reorders imports so collection resolves paths first), the
production default reappears and these fail loudly.
"""
from pathlib import Path

from ccswitch import paths
from server.config import get_settings

_REAL_HOME = (Path.home() / ".claude-accounts").resolve()


def test_ccswitch_home_is_not_production():
    root = paths.root().resolve()
    assert root != _REAL_HOME, (
        f"CCSWITCH_HOME resolves to the REAL store {_REAL_HOME}; tests must use a temp dir"
    )
    assert _REAL_HOME not in root.parents


def test_db_url_is_not_production():
    db_url = get_settings().db_url
    assert str(_REAL_HOME) not in db_url, (
        f"DB URL {db_url!r} points at the real account DB; tests must use a temp DB"
    )


def test_ledger_and_slot_paths_are_isolated():
    assert paths.ledger_path().resolve() != (_REAL_HOME / "ledger.json")
    assert _REAL_HOME not in paths.ledger_path().resolve().parents
    assert _REAL_HOME not in paths.slot_path("primary").resolve().parents
