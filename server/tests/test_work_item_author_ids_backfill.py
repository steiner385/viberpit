"""Startup backfill: the mirror gains ``work_items.author_id`` (#3147), so every repo is
re-mirrored ONCE to fill it. Authors are NOT cleared and no work_items row is touched --
only ``ProjectRepo`` backfill state is reset. One-shot behind a ``data_backfills`` marker.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy import Table, create_engine, select

from server.db import _backfill_work_item_author_ids, init_db
from server.db.models import Base, DataBackfill, ProjectRepo, WorkItem

_T: Table = WorkItem.__table__  # type: ignore[assignment]
_R: Table = ProjectRepo.__table__  # type: ignore[assignment]
_NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def _seed(engine) -> None:
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(_T.insert().values(
            id="1", repo="o/r", number=1, kind="issue", author="tony",
            last_synced_at=_NOW))
        conn.execute(_R.insert().values(
            id="pr1", repo="o/r", backfill_done=True, backfill_cursor="CUR",
            backfill_phase="prs"))


def _repo_state(engine) -> tuple:
    with engine.connect() as conn:
        return tuple(conn.execute(select(
            _R.c.backfill_done, _R.c.backfill_cursor, _R.c.backfill_phase)).one())


def _items(engine) -> list[tuple]:
    with engine.connect() as conn:
        return [tuple(r) for r in conn.execute(
            select(_T.c.id, _T.c.author, _T.c.author_id))]


def _markers(engine) -> list[str]:
    with engine.connect() as conn:
        return [r[0] for r in conn.execute(select(DataBackfill.__table__.c.name))]


def test_backfill_resets_repos_keeps_items_and_records_a_marker(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/b.db")
    _seed(engine)
    with engine.begin() as conn:
        _backfill_work_item_author_ids(conn)
    assert _repo_state(engine) == (False, None, "issues")
    assert _items(engine) == [("1", "tony", None)]  # authors untouched
    assert _markers(engine) == ["work_item_author_ids"]


def test_second_run_is_a_no_op(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/b.db")
    _seed(engine)
    with engine.begin() as conn:
        _backfill_work_item_author_ids(conn)
    with engine.begin() as conn:
        conn.execute(_R.update().values(backfill_done=True, backfill_phase="prs"))
    with engine.begin() as conn:
        _backfill_work_item_author_ids(conn)
    assert _repo_state(engine) == (True, None, "prs")


def test_init_db_runs_the_backfill_once_on_startup(isolated_db):
    path = isolated_db.removeprefix("sqlite+aiosqlite:///")
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as conn:
        conn.execute(DataBackfill.__table__.delete())
    _seed(engine)
    engine.dispose()
    asyncio.run(init_db(isolated_db))
    engine = create_engine(f"sqlite:///{path}")
    assert _repo_state(engine) == (False, None, "issues")
    with engine.begin() as conn:
        conn.execute(_R.update().values(backfill_done=True, backfill_phase="prs"))
    engine.dispose()
    asyncio.run(init_db(isolated_db))  # a second boot is a no-op
    engine = create_engine(f"sqlite:///{path}")
    assert _repo_state(engine) == (True, None, "prs")
    engine.dispose()
