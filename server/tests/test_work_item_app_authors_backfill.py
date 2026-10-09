"""Startup backfill: pre-bound bare App logins on mirrored work items become ``app/<slug>``.

Production runs ``init_db`` (create_all + reconcile), never ``alembic upgrade``, so the
0081 data migration only reaches dev/test DBs; this startup helper is what converts the
live rows. It is bounded by ``gh_created_at`` so a bare row created after the logins were
verified unregistered (i.e. a squatting user) is never relabelled as the App.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy import create_engine, select

from server.db import _backfill_app_authors, init_db
from server.db.models import Base, WorkItem

_BEFORE = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
_AFTER = datetime(2026, 10, 10, 0, 0, 1, tzinfo=timezone.utc)


def _seed(engine) -> None:
    Base.metadata.create_all(engine)
    rows = [
        ("1", "github-actions", _BEFORE),
        ("2", "viberpit", _BEFORE),
        ("3", "github-actions", _AFTER),   # squatting user after the bound: stays bare
        ("4", "viberpit", None),           # unknown creation time: stays bare (fail closed)
        ("5", "copilot-swe-agent", _BEFORE),  # a registered user login: never converted
        ("6", "tony", _BEFORE),
        ("7", "app/github-actions", _BEFORE),
    ]
    with engine.begin() as conn:
        for id_, author, created in rows:
            conn.execute(WorkItem.__table__.insert().values(
                id=id_, repo="o/r", number=int(id_), kind="issue",
                author=author, gh_created_at=created))


def _authors(engine) -> list[str | None]:
    with engine.connect() as conn:
        return [r[0] for r in conn.execute(
            select(WorkItem.__table__.c.author).order_by(WorkItem.__table__.c.id))]


EXPECTED = ["app/github-actions", "app/viberpit", "github-actions", "viberpit",
            "copilot-swe-agent", "tony", "app/github-actions"]


def test_backfill_converts_pre_bound_bare_app_rows_only_and_is_idempotent(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/b.db")
    _seed(engine)
    with engine.begin() as conn:
        _backfill_app_authors(conn)
    assert _authors(engine) == EXPECTED
    with engine.begin() as conn:
        _backfill_app_authors(conn)
    assert _authors(engine) == EXPECTED


def test_init_db_runs_the_backfill_on_startup(isolated_db):
    path = isolated_db.removeprefix("sqlite+aiosqlite:///")
    engine = create_engine(f"sqlite:///{path}")
    _seed(engine)
    engine.dispose()
    asyncio.run(init_db(isolated_db))
    asyncio.run(init_db(isolated_db))  # a second boot is a no-op
    engine = create_engine(f"sqlite:///{path}")
    assert _authors(engine) == EXPECTED
    engine.dispose()
