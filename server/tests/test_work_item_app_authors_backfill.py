"""Startup backfill: bare App logins the OLD mirror wrote become ``app/<slug>``, ONCE.

Production runs ``init_db`` (create_all + reconcile), never ``alembic upgrade``, so the
0081 data migration only reaches dev/test DBs; this startup helper converts the live
rows. It is one-shot behind a ``data_backfills`` marker because GitHub users can
rename: a user who authored an old issue can later rename to the unregistered
``viberpit``/``github-actions`` login, the next sync stores that bare login on an OLD
row, and a repeated relabel would make them a trusted App author.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import Table, create_engine, select

from server.db import _backfill_app_authors, init_db
from server.db.models import Base, DataBackfill, WorkItem

_BEFORE = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
_AFTER = datetime(2026, 10, 10, 0, 0, 1, tzinfo=timezone.utc)
_FUTURE = datetime.now(timezone.utc) + timedelta(days=1)

_T: Table = WorkItem.__table__  # type: ignore[assignment]


def _insert(engine, id_, author, created, synced=_BEFORE) -> None:
    with engine.begin() as conn:
        conn.execute(_T.insert().values(
            id=id_, repo="o/r", number=int(id_), kind="issue",
            author=author, gh_created_at=created, last_synced_at=synced))


def _seed(engine) -> None:
    Base.metadata.create_all(engine)
    for row in [
        ("1", "github-actions", _BEFORE),
        ("2", "viberpit", _BEFORE),
        ("3", "github-actions", _AFTER),      # created after the bound: stays bare
        ("4", "viberpit", None),              # unknown creation time: stays bare
        ("5", "copilot-swe-agent", _BEFORE),  # a registered user login: never converted
        ("6", "tony", _BEFORE),
        ("7", "app/github-actions", _BEFORE),  # already converted: untouched
    ]:
        _insert(engine, *row)
    # Written by the mirror after this run started (can only be the new sync code).
    _insert(engine, "8", "viberpit", _BEFORE, synced=_FUTURE)


def _authors(engine) -> list[str | None]:
    with engine.connect() as conn:
        return [r[0] for r in conn.execute(select(_T.c.author).order_by(_T.c.number))]


EXPECTED = ["app/github-actions", "app/viberpit", "github-actions", "viberpit",
            "copilot-swe-agent", "tony", "app/github-actions", "viberpit"]


def test_backfill_converts_old_bare_app_rows_once_and_records_a_marker(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/b.db")
    _seed(engine)
    with engine.begin() as conn:
        _backfill_app_authors(conn)
    assert _authors(engine) == EXPECTED
    with engine.connect() as conn:
        assert [r[0] for r in conn.execute(select(DataBackfill.__table__.c.name))] == [
            "work_item_app_authors"]


def test_second_run_is_a_no_op_even_for_a_renamed_user_on_an_old_row(tmp_path):
    """The rename attack: after the first run, a user who opened an old issue renames
    to ``viberpit``; the sync stores the bare login on that old row. A second run must
    NOT relabel it (the marker), whatever its dates say."""
    engine = create_engine(f"sqlite:///{tmp_path}/b.db")
    _seed(engine)
    with engine.begin() as conn:
        _backfill_app_authors(conn)
    _insert(engine, "9", "viberpit", _BEFORE, synced=_BEFORE)
    _insert(engine, "10", "github-actions", _BEFORE, synced=_BEFORE)
    with engine.begin() as conn:
        _backfill_app_authors(conn)
    assert _authors(engine) == EXPECTED + ["viberpit", "github-actions"]


def test_init_db_runs_the_backfill_once_on_startup(isolated_db):
    path = isolated_db.removeprefix("sqlite+aiosqlite:///")
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as conn:  # isolated_db's eager init already wrote the marker
        conn.execute(DataBackfill.__table__.delete())
    _seed(engine)
    engine.dispose()
    asyncio.run(init_db(isolated_db))
    engine = create_engine(f"sqlite:///{path}")
    assert _authors(engine) == EXPECTED
    _insert(engine, "9", "viberpit", _BEFORE, synced=_BEFORE)  # rename attack row
    engine.dispose()
    asyncio.run(init_db(isolated_db))  # a second boot is a no-op
    engine = create_engine(f"sqlite:///{path}")
    assert _authors(engine) == EXPECTED + ["viberpit"]
    engine.dispose()
