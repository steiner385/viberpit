"""Startup backfill: bare App-looking authors the OLD mirror wrote are CLEARED and every
repo is re-mirrored, ONCE -- nothing is ever relabelled ``app/<slug>`` from a login.

Production runs ``init_db`` (create_all + reconcile), never ``alembic upgrade``, so the
0082 data migration only reaches dev/test DBs; this startup helper fixes the live rows.

A stored login is a stale identity: it says what the account was CALLED when the row
was last synced, not whether it was the App. Someone who registered -- or renamed to --
``viberpit``/``github-actions`` before this deploy has bare rows indistinguishable from
the App's, so inferring ``app/`` from the name would hand them the App's trust. The
backfill therefore clears those authors (untrusted, fail closed) and resets each repo's
mirror backfill, so the new sync re-reads every author with ``__typename`` and GitHub
itself says which rows are Bots. One-shot behind a ``data_backfills`` marker.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy import Table, create_engine, select

from server.db import _backfill_app_authors, init_db
from server.db.models import Base, DataBackfill, ProjectRepo, WorkItem

_BEFORE = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
_AFTER = datetime(2026, 10, 10, 0, 0, 1, tzinfo=timezone.utc)

_T: Table = WorkItem.__table__  # type: ignore[assignment]
_R: Table = ProjectRepo.__table__  # type: ignore[assignment]


def _insert(engine, id_, author, created=_BEFORE) -> None:
    with engine.begin() as conn:
        conn.execute(_T.insert().values(
            id=id_, repo="o/r", number=int(id_), kind="issue",
            author=author, gh_created_at=created, last_synced_at=_BEFORE))


def _seed(engine) -> None:
    Base.metadata.create_all(engine)
    for row in [
        ("1", "github-actions", _BEFORE),
        ("2", "viberpit", _BEFORE),
        ("3", "github-actions", _AFTER),      # dates don't matter: the name proves nothing
        ("4", "viberpit", None),
        ("5", "copilot-swe-agent", _BEFORE),  # not an App-looking name: untouched
        ("6", "tony", _BEFORE),
        ("7", "app/github-actions", _BEFORE),  # already typed by the new sync: untouched
    ]:
        _insert(engine, *row)
    with engine.begin() as conn:
        conn.execute(_R.insert().values(
            id="pr1", repo="o/r", backfill_done=True, backfill_cursor="CUR",
            backfill_phase="prs"))


def _authors(engine) -> list[str | None]:
    with engine.connect() as conn:
        return [r[0] for r in conn.execute(select(_T.c.author).order_by(_T.c.number))]


def _repo_state(engine) -> tuple:
    with engine.connect() as conn:
        return tuple(conn.execute(select(
            _R.c.backfill_done, _R.c.backfill_cursor, _R.c.backfill_phase)).one())


EXPECTED = [None, None, None, None, "copilot-swe-agent", "tony", "app/github-actions"]


def test_backfill_clears_bare_app_names_resyncs_and_records_a_marker(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/b.db")
    _seed(engine)
    with engine.begin() as conn:
        _backfill_app_authors(conn)
    assert _authors(engine) == EXPECTED
    # Never inferred: the only ``app/`` row is the one the new sync wrote itself.
    assert [a for a in _authors(engine) if a and a.startswith("app/")] == ["app/github-actions"]
    assert _repo_state(engine) == (False, None, "issues")   # re-mirror from scratch
    with engine.connect() as conn:
        assert [r[0] for r in conn.execute(select(DataBackfill.__table__.c.name))] == [
            "work_item_app_authors"]


def test_second_run_is_a_no_op(tmp_path):
    """After the first run, a bare ``viberpit`` row can only have come from the NEW sync
    (which writes Bots as ``app/<slug>``), so it is a USER of that name -- a squatter or
    a renamed account. A second run must leave it, and the repos, alone."""
    engine = create_engine(f"sqlite:///{tmp_path}/b.db")
    _seed(engine)
    with engine.begin() as conn:
        _backfill_app_authors(conn)
    _insert(engine, "9", "viberpit")
    with engine.begin() as conn:
        conn.execute(_R.update().values(backfill_done=True, backfill_phase="prs"))
    with engine.begin() as conn:
        _backfill_app_authors(conn)
    assert _authors(engine) == EXPECTED + ["viberpit"]
    assert _repo_state(engine) == (True, None, "prs")


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
    _insert(engine, "9", "viberpit")  # a user of that name, synced after the backfill
    engine.dispose()
    asyncio.run(init_db(isolated_db))  # a second boot is a no-op
    engine = create_engine(f"sqlite:///{path}")
    assert _authors(engine) == EXPECTED + ["viberpit"]
    engine.dispose()
