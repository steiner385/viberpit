"""Per-viewer read state for sessions (D-10).

A session is unread for a viewer iff that viewer has a ``session_views`` row AND the
session's ``last_turn_at`` is later than its ``viewed_at``. No row = read, so existing
and automation-only sessions don't turn bold the day this ships. The list endpoints
compute ``unread`` for the calling identity in ONE batched query per request; the two
POST routes write the row.
"""
from __future__ import annotations

import importlib.util
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, event, inspect, text

from tests.helpers import temp_env
from server.tests.test_archive_reconcile_api import _app_with_orch, _create

A = {"Tailscale-User-Login": "alice@example.com"}
B = {"Tailscale-User-Login": "bob@example.com"}

MIGRATION = Path(__file__).resolve().parents[1] / "db" / "migrations" / "versions" / \
    "0096_session_views.py"


def _db(d) -> sqlite3.Connection:
    return sqlite3.connect(str(d / "ccswitch.db"))


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S.%f")


def _set_last_turn(d, sid: str, when: datetime | None) -> None:
    with _db(d) as conn:
        conn.execute("UPDATE sessions SET last_turn_at = ? WHERE id = ?",
                     (None if when is None else _fmt(when), sid))


def _viewed_at(d, identity: str, sid: str) -> datetime | None:
    with _db(d) as conn:
        row = conn.execute(
            "SELECT viewed_at FROM session_views WHERE identity = ? AND session_id = ?",
            (identity, sid)).fetchone()
    if row is None:
        return None
    return datetime.fromisoformat(row[0])


def _unread(client, sid: str, headers=A, path: str = "/api/sessions") -> bool:
    resp = client.get(path, headers=headers)
    assert resp.status_code == 200, resp.text
    [row] = [s for s in resp.json() if s["id"] == sid]
    return row["unread"]


def _post(client, sid: str, action: str, headers=A) -> int:
    return client.post(f"/api/sessions/{sid}/{action}", headers=headers).status_code


def test_no_row_means_read_even_after_a_turn():
    with temp_env() as d, _app_with_orch(d) as (client, _orch):
        sid = _create(client, "Fresh")
        _set_last_turn(d, sid, datetime.utcnow())
        assert _unread(client, sid) is False


def test_viewed_then_later_turn_then_viewed_then_mark_unread():
    with temp_env() as d, _app_with_orch(d) as (client, _orch):
        sid = _create(client, "Cycle")
        assert _post(client, sid, "viewed") == 204
        viewed = _viewed_at(d, "alice@example.com", sid)
        assert viewed is not None
        assert _unread(client, sid) is False

        _set_last_turn(d, sid, viewed + timedelta(minutes=1))
        assert _unread(client, sid) is True

        # Viewed again after that turn: the DB clock is now past it.
        _set_last_turn(d, sid, datetime.utcnow() - timedelta(minutes=1))
        assert _post(client, sid, "viewed") == 204
        assert _unread(client, sid) is False

        assert _post(client, sid, "unread") == 204
        assert _unread(client, sid) is True


def test_mark_unread_sets_viewed_just_before_the_last_turn():
    with temp_env() as d, _app_with_orch(d) as (client, _orch):
        sid = _create(client, "Precise")
        turn = datetime(2026, 11, 3, 12, 0, 0, 500000)
        _set_last_turn(d, sid, turn)
        assert _post(client, sid, "unread") == 204
        assert _viewed_at(d, "alice@example.com", sid) == turn - timedelta(microseconds=1)
        assert _unread(client, sid) is True


def test_mark_unread_without_a_turn_deletes_the_row():
    with temp_env() as d, _app_with_orch(d) as (client, _orch):
        sid = _create(client, "NoTurn")
        _set_last_turn(d, sid, None)
        assert _post(client, sid, "viewed") == 204
        assert _viewed_at(d, "alice@example.com", sid) is not None
        assert _post(client, sid, "unread") == 204
        assert _viewed_at(d, "alice@example.com", sid) is None
        assert _unread(client, sid) is False


def test_one_viewers_state_never_leaks_to_another():
    with temp_env() as d, _app_with_orch(d) as (client, _orch):
        sid = _create(client, "Shared")
        _set_last_turn(d, sid, datetime(2026, 1, 1))
        assert _post(client, sid, "unread", headers=A) == 204
        assert _unread(client, sid, headers=A) is True
        assert _unread(client, sid, headers=B) is False
        assert _viewed_at(d, "bob@example.com", sid) is None


def test_archived_sessions_carry_unread():
    with temp_env() as d, _app_with_orch(d) as (client, _orch):
        sid = _create(client, "Old")
        other = _create(client, "OldToo")
        _set_last_turn(d, sid, datetime(2026, 1, 1))
        _set_last_turn(d, other, datetime(2026, 1, 1))
        assert _post(client, sid, "unread") == 204
        for s in (sid, other):
            assert client.delete(f"/api/sessions/{s}", headers=A).status_code == 204
        assert _unread(client, sid, path="/api/sessions/archived") is True
        assert _unread(client, other, path="/api/sessions/archived") is False
        assert _unread(client, sid, headers=B, path="/api/sessions/archived") is False


def test_permanent_delete_drops_every_viewers_row():
    """SQLite doesn't enforce the ON DELETE CASCADE, so the purge deletes explicitly."""
    with temp_env() as d, _app_with_orch(d) as (client, _orch):
        sid = _create(client, "Doomed")
        assert _post(client, sid, "viewed", headers=A) == 204
        assert _post(client, sid, "viewed", headers=B) == 204
        resp = client.post("/api/sessions/batch/permanent-delete",
                           json={"ids": [sid]}, headers=A)
        assert resp.status_code == 200, resp.text
        assert _viewed_at(d, "alice@example.com", sid) is None
        assert _viewed_at(d, "bob@example.com", sid) is None


def _count_read_state_queries(client, path: str) -> int:
    import server.db as db_module

    seen: list[str] = []

    def _spy(conn, cursor, statement, parameters, context, executemany):  # noqa: ARG001
        if "session_views" in statement and statement.lstrip().upper().startswith("SELECT"):
            seen.append(statement)

    assert db_module._engine is not None
    engine = db_module._engine.sync_engine
    event.listen(engine, "before_cursor_execute", _spy)
    try:
        assert client.get(path, headers=A).status_code == 200
    finally:
        event.remove(engine, "before_cursor_execute", _spy)
    return len(seen)


def test_list_endpoints_issue_one_read_state_query_regardless_of_session_count():
    with temp_env() as d, _app_with_orch(d) as (client, _orch):
        first = _create(client, "One")
        assert _post(client, first, "viewed") == 204
        assert _count_read_state_queries(client, "/api/sessions") == 1
        for name in ("Two", "Three", "Four"):
            assert _post(client, _create(client, name), "viewed") == 204
        assert _count_read_state_queries(client, "/api/sessions") == 1
        assert _count_read_state_queries(client, "/api/sessions/archived") == 1


def test_unknown_session_is_404():
    with temp_env() as d, _app_with_orch(d) as (client, _orch):
        assert _post(client, "no-such-session", "viewed") == 404
        assert _post(client, "no-such-session", "unread") == 404


# --------------------------------------------------------------------------- #
# Migration                                                                   #
# --------------------------------------------------------------------------- #

def _module():
    spec = importlib.util.spec_from_file_location("session_views_migration", MIGRATION)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(engine, fn) -> None:
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            fn()


def test_migration_round_trips(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/m.db")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE sessions (id TEXT PRIMARY KEY)"))
    mod = _module()

    _run(engine, mod.upgrade)
    inspector = inspect(engine)
    assert "session_views" in inspector.get_table_names()
    cols = {c["name"]: c for c in inspector.get_columns("session_views")}
    assert set(cols) == {"identity", "session_id", "viewed_at"}
    assert inspector.get_pk_constraint("session_views")["constrained_columns"] == [
        "identity", "session_id"]
    assert any(ix["column_names"] == ["identity"]
               for ix in inspector.get_indexes("session_views"))

    _run(engine, mod.downgrade)
    assert "session_views" not in inspect(engine).get_table_names()

    _run(engine, mod.upgrade)
    assert "session_views" in inspect(engine).get_table_names()
