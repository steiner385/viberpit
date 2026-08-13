"""Tests for Session.archived_at column and list_recent_archived_rows.

Covers:
  - archive_session sets archived_at to a non-None timestamp (~now).
  - list_recent_archived_rows returns only rows with archived_at >= since,
    excludes NULL archived_at rows, and returns the correct dict shape.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime
from unittest.mock import patch



# ---------------------------------------------------------------------------
# archive_session sets archived_at
# ---------------------------------------------------------------------------


def test_archive_session_sets_archived_at(tmp_path):
    """archive_session must set archived_at to a non-None datetime on the DB row."""
    from server.db import init_db, db_session
    from server.db.models import Session as DbSession
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory

    db_url = f"sqlite+aiosqlite:///{tmp_path / 'archat_test.db'}"
    cwd = tmp_path / "sess_cwd"
    cwd.mkdir()

    async def run():
        with patch.dict(os.environ, {"CCSWITCH_HOME": str(tmp_path)}):
            await init_db(db_url=db_url)
            async with db_session() as db:
                db.add(DbSession(
                    id="s1", name="S1", account_name="acct1",
                    claude_session_id=None, status="idle", cwd=str(cwd),
                ))
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
            from server.sessions.types import SessionRecord
            orch._records["s1"] = SessionRecord(
                id="s1", name="S1", account="acct1",
                claude_session_id="", cols=80, rows=24, cwd=str(cwd),
            )
            t_before = datetime.utcnow()
            await orch.archive_session("s1")
            t_after = datetime.utcnow()

            from sqlalchemy import select as sa_select
            async with db_session() as db:
                row = (await db.execute(
                    sa_select(DbSession).where(DbSession.id == "s1")
                )).scalars().first()
            return row.archived_at, t_before, t_after

    archived_at, t_before, t_after = asyncio.run(run())
    assert archived_at is not None, "archived_at must be set after archive_session"
    assert t_before <= archived_at <= t_after, (
        f"archived_at={archived_at} should be between {t_before} and {t_after}"
    )


# ---------------------------------------------------------------------------
# list_recent_archived_rows — FakeRow-based (no real DB)
# ---------------------------------------------------------------------------


def _make_fake_row(*, id, archived_at, active_goal="fix ci", inferred_goal=None,
                   repo="o/r", branch="feat/x", authored_branches=None,
                   initial_prompt=None):
    class Row:
        pass
    r = Row()
    r.id = id
    r.archived_at = archived_at
    r.active_goal = active_goal
    r.inferred_goal = inferred_goal
    r.repo = repo
    r.branch = branch
    r.authored_branches = authored_branches if authored_branches is not None else [branch]
    r.name = id
    r.initial_prompt = initial_prompt
    return r


def _make_windowed_factory(all_rows, since):
    rows_in_window = [
        r for r in all_rows
        if r.archived_at is not None and r.archived_at >= since
    ]

    class FakeScalars:
        def all(self):
            return rows_in_window

    class FakeResult:
        def scalars(self):
            return FakeScalars()

    class FakeDb:
        async def execute(self, _q):
            return FakeResult()

    class FakeFactory:
        def __call__(self):
            return self

        async def __aenter__(self):
            return FakeDb()

        async def __aexit__(self, *a):
            return False

    return FakeFactory()


def test_list_recent_archived_rows_returns_in_window_rows():
    """list_recent_archived_rows returns rows with archived_at >= since."""
    from server.automations import archive_audit as aa

    since = datetime(2026, 1, 1, 12, 0, 0)
    in_window = _make_fake_row(id="s-in", archived_at=datetime(2026, 1, 2))
    factory = _make_windowed_factory([in_window], since)

    rows = asyncio.run(aa.list_recent_archived_rows(factory, since))
    assert len(rows) == 1
    assert rows[0]["id"] == "s-in"


def test_list_recent_archived_rows_excludes_null_archived_at():
    """Rows with archived_at=None (pre-column rows) must not appear."""
    from server.automations import archive_audit as aa

    since = datetime(2026, 1, 1)
    null_row = _make_fake_row(id="s-null", archived_at=None)
    factory = _make_windowed_factory([null_row], since)

    rows = asyncio.run(aa.list_recent_archived_rows(factory, since))
    assert rows == []


def test_list_recent_archived_rows_excludes_before_window():
    """Rows with archived_at before since must be excluded."""
    from server.automations import archive_audit as aa

    since = datetime(2026, 6, 1)
    old_row = _make_fake_row(id="s-old", archived_at=datetime(2026, 1, 1))
    factory = _make_windowed_factory([old_row], since)

    rows = asyncio.run(aa.list_recent_archived_rows(factory, since))
    assert rows == []


def test_list_recent_archived_rows_dict_shape():
    """Returned dicts have the same fields as load_archived_rows."""
    from server.automations import archive_audit as aa

    since = datetime(2026, 1, 1)
    row = _make_fake_row(
        id="s-shape", archived_at=datetime(2026, 3, 1),
        active_goal="fix ci", repo="o/r", branch="feat/fix",
        authored_branches=["feat/fix", "feat/extra"],
        initial_prompt="Fix the pipeline",
    )
    factory = _make_windowed_factory([row], since)

    rows = asyncio.run(aa.list_recent_archived_rows(factory, since))
    assert len(rows) == 1
    d = rows[0]
    assert d["id"] == "s-shape"
    assert d["repo"] == "o/r"
    assert d["branch"] == "feat/fix"
    assert d["authored_branches"] == ["feat/fix", "feat/extra"]
    assert d["goal"] == "fix ci"
    assert "goal_source" in d
    assert d["initial_prompt"] == "Fix the pipeline"


def test_list_recent_archived_rows_boundary_inclusive():
    """A row exactly at since (archived_at == since) must be included."""
    from server.automations import archive_audit as aa

    since = datetime(2026, 3, 15, 10, 0, 0)
    exact_row = _make_fake_row(id="s-exact", archived_at=since)
    factory = _make_windowed_factory([exact_row], since)

    rows = asyncio.run(aa.list_recent_archived_rows(factory, since))
    assert len(rows) == 1
    assert rows[0]["id"] == "s-exact"


def test_list_recent_archived_rows_mixed_window():
    """Mixed set: in-window, NULL, and before-window — only in-window returns."""
    from server.automations import archive_audit as aa

    since = datetime(2026, 6, 1)
    in_row = _make_fake_row(id="s-in", archived_at=datetime(2026, 6, 15))
    null_row = _make_fake_row(id="s-null", archived_at=None)
    old_row = _make_fake_row(id="s-old", archived_at=datetime(2026, 1, 1))
    factory = _make_windowed_factory([in_row, null_row, old_row], since)

    rows = asyncio.run(aa.list_recent_archived_rows(factory, since))
    assert len(rows) == 1
    assert rows[0]["id"] == "s-in"


def test_list_recent_archived_rows_real_db_where_clause(tmp_path):
    """Real DB: the actual SQL WHERE (status='archived' AND archived_at not NULL AND
    archived_at >= since AND archived_by IN AUTO_ARCHIVE_PROVENANCE) must do the
    filtering. The fake-factory tests above simulate the filter in Python and so cannot
    catch a wrong query; this seeds a temp DB and exercises the real select. Every
    candidate row carries an auto archived_by so it is excluded only by the dimension
    under test (window/status); manual + NULL provenance exclusion is covered in
    test_archive_audit.py."""
    from server.automations import archive_audit as aa
    from server.db import init_db, db_session
    from server.db.models import Session as DbSession

    since = datetime(2026, 6, 1)

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'window.db'}")
        async with db_session() as db:
            db.add_all([
                DbSession(id="in", name="in", account_name="a", status="archived",
                          cwd="/t", repo="o/r", branch="feat/in", archived_by="autopilot",
                          archived_at=datetime(2026, 6, 15)),
                DbSession(id="old", name="old", account_name="a", status="archived",
                          cwd="/t", repo="o/r", branch="feat/old", archived_by="autopilot",
                          archived_at=datetime(2026, 1, 1)),
                DbSession(id="null", name="null", account_name="a", status="archived",
                          cwd="/t", repo="o/r", branch="feat/null", archived_by="autopilot",
                          archived_at=None),
                # in-window timestamp but NOT archived → excluded by the status filter
                DbSession(id="live", name="live", account_name="a", status="idle",
                          cwd="/t", repo="o/r", branch="feat/live", archived_by="autopilot",
                          archived_at=datetime(2026, 6, 20)),
            ])
        return await aa.list_recent_archived_rows(db_session, since)

    rows = asyncio.run(run())
    assert {r["id"] for r in rows} == {"in"}
