"""``AutomationContext`` dispatch-outcome seams (spec 2026-09-12 product automation
policy §6, §13; phase 3 task 1).

The two writers of ``dispatch_outcomes``: ``record_outcome`` at spawn (a ``running``
row) and ``finalize_outcome`` at each terminal branch of the runner's sweep. Both are
best-effort by §13 — history must never take a dispatch down with it — so the DB
failure path is covered explicitly. The one thing that DOES raise is an outcome
outside the vocabulary: that is a programming error, not a blip.

Real SQLite, no GitHub: the fingerprint is read off the mirror ``WorkItem`` row, so a
seeded mirror is the point.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from server.automations.context import AutomationContext
from server.db import db_session, init_db
from server.db.models import DispatchOutcome, Project, ProjectRepo, WorkItem
from server.pm.outcomes import body_fingerprint, bounces_stmt, rows_to_bounces

REPO = "o/r"


@pytest.fixture
def db_env(tmp_path):
    """A private SQLite file per test; ``_seed`` initialises it inside the test's own
    event loop (an engine built in another loop cannot be used from this one)."""
    return f"sqlite+aiosqlite:///{tmp_path / 'outcomes.db'}"


async def _seed(db_url: str) -> None:
    await init_db(db_url=db_url)
    async with db_session() as db:
        project = Project(slug="viberpit", name="viberpit")
        db.add(project)
        await db.flush()
        db.add(ProjectRepo(project_id=project.id, repo=REPO, is_primary=True))
        db.add(WorkItem(repo=REPO, number=7, kind="issue", title="T", body="B"))


async def _rows() -> list[DispatchOutcome]:
    async with db_session() as db:
        return list((await db.execute(
            select(DispatchOutcome).order_by(DispatchOutcome.created_at))).scalars().all())


@pytest.mark.asyncio
async def test_record_then_finalize_writes_one_row(db_env):
    await _seed(db_env)
    ctx = AutomationContext("backlog-runner")
    await ctx.record_outcome("o/r#7", product_slug="viberpit", session_id=None,
                             model="sonnet", attempt_no=0)
    await ctx.finalize_outcome("o/r#7", outcome="no_pr", cause="session ended")

    rows = await _rows()
    assert len(rows) == 1
    r = rows[0]
    assert r.outcome == "no_pr" and r.cause == "session ended" and r.ended_at is not None
    assert r.body_fingerprint == body_fingerprint("T", "B")
    assert r.project_id is not None and r.attempt_no == 0 and r.model == "sonnet"
    assert r.duration_s is not None and r.duration_s >= 0
    assert r.updated_at is not None and r.target_ref == "o/r#7"


@pytest.mark.asyncio
async def test_finalize_touches_only_the_newest_running_row(db_env):
    """Two attempts on one issue leave two rows; each finalise claims the newest
    still-running one, so an in-flight attempt is never stamped with the previous
    attempt's outcome."""
    await _seed(db_env)
    ctx = AutomationContext("backlog-runner")
    await ctx.record_outcome("o/r#7", product_slug="viberpit", session_id=None,
                             model="sonnet", attempt_no=0)
    await ctx.record_outcome("o/r#7", product_slug="viberpit", session_id=None,
                             model="opus", attempt_no=1)

    await ctx.finalize_outcome("o/r#7", outcome="merged", pr_number=42,
                               pr_url="https://example/pr/42", first_pass_merge=True)
    older, newer = await _rows()
    assert newer.attempt_no == 1 and newer.outcome == "merged"
    assert newer.pr_number == 42 and newer.pr_url == "https://example/pr/42"
    assert newer.first_pass_merge is True
    assert older.attempt_no == 0 and older.outcome == "running" and older.ended_at is None

    await ctx.finalize_outcome("o/r#7", outcome="needs_human", cause="escalated")
    older, newer = await _rows()
    assert older.outcome == "needs_human" and older.cause == "escalated"
    assert newer.outcome == "merged"


@pytest.mark.asyncio
async def test_finalize_without_a_running_row_is_a_no_op(db_env):
    await _seed(db_env)
    ctx = AutomationContext("backlog-runner")
    await ctx.finalize_outcome("o/r#404", outcome="abandoned", cause="never dispatched")
    assert await _rows() == []


@pytest.mark.asyncio
async def test_finalize_rejects_an_unknown_outcome(db_env):
    await _seed(db_env)
    ctx = AutomationContext("backlog-runner")
    with pytest.raises(ValueError):
        await ctx.finalize_outcome("o/r#7", outcome="exploded")


@pytest.mark.asyncio
async def test_record_survives_a_missing_mirror_row_and_unknown_product(db_env):
    """No mirror row → no fingerprint (so the issue can never be counted as bounced
    against a body nobody has seen); an unknown product slug → no project_id. Neither
    is an error: the dispatch still happened and still deserves a row."""
    await _seed(db_env)
    ctx = AutomationContext("backlog-runner")
    await ctx.record_outcome("o/r#999", product_slug="nope", session_id=None,
                             model=None, attempt_no=0)
    rows = await _rows()
    assert len(rows) == 1
    assert rows[0].body_fingerprint is None and rows[0].project_id is None
    assert rows[0].outcome == "running" and rows[0].started_at is not None


@pytest.mark.asyncio
async def test_writes_are_best_effort(db_env, monkeypatch):
    """Spec §13: an outcome write that fails is logged, never raised — a dead history
    table must not stop the runner spawning or sweeping."""
    await _seed(db_env)
    ctx = AutomationContext("backlog-runner")

    from server.automations import context as ctx_mod

    def boom(*a, **kw):
        raise RuntimeError("db is down")

    monkeypatch.setattr(ctx_mod, "db_session", boom)
    await ctx.record_outcome("o/r#7", product_slug="viberpit", session_id=None,
                             model="sonnet", attempt_no=0)
    await ctx.finalize_outcome("o/r#7", outcome="no_pr", cause="session ended")
    monkeypatch.undo()
    assert await _rows() == []


@pytest.mark.asyncio
async def test_bounces_stmt_counts_finalised_rows_for_the_current_body(db_env):
    """The query builder against a real dialect: two finalised non-merged rows on the
    current body count, the ``running`` row and a row carrying the pre-edit
    fingerprint do not."""
    await _seed(db_env)
    ctx = AutomationContext("backlog-runner")
    for outcome in ("no_pr", "pr_gave_up"):
        await ctx.record_outcome("o/r#7", product_slug="viberpit", session_id=None,
                                 model="sonnet", attempt_no=0)
        await ctx.finalize_outcome("o/r#7", outcome=outcome)
    await ctx.record_outcome("o/r#7", product_slug="viberpit", session_id=None,
                             model="sonnet", attempt_no=2)   # still running
    async with db_session() as db:
        db.add(DispatchOutcome(target_ref="o/r#7", body_fingerprint="stale",
                               outcome="no_pr"))

    fingerprint = body_fingerprint("T", "B")
    pairs = [("o/r#7", fingerprint), ("o/r#8", fingerprint)]
    async with db_session() as db:
        rows = (await db.execute(bounces_stmt(pairs))).all()
    assert rows_to_bounces(rows, pairs) == {"o/r#7": 2, "o/r#8": 0}


@pytest.mark.asyncio
async def test_finalize_breaks_a_created_at_tie_deterministically(db_env):
    """Two dispatches of one target CAN overlap (a propose tap racing the loop's
    own spawn, or a double tap), and two rows can land on the same ``created_at``.
    ``id`` is the tiebreak, so which of them a finalise claims is decided, not
    dialect-dependent — and the second finalise claims the other one rather than
    re-stamping the first."""
    from datetime import datetime, timezone

    await _seed(db_env)
    ctx = AutomationContext("backlog-runner")
    await ctx.record_outcome("o/r#7", product_slug="viberpit", session_id=None,
                             model="sonnet", attempt_no=0)
    await ctx.record_outcome("o/r#7", product_slug="viberpit", session_id=None,
                             model="sonnet", attempt_no=1)
    stamp = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
    async with db_session() as db:
        for row in (await db.execute(select(DispatchOutcome))).scalars().all():
            row.created_at = stamp

    ids = sorted(r.id for r in await _rows())
    await ctx.finalize_outcome("o/r#7", outcome="no_pr", cause="first")
    by_id = {r.id: r for r in await _rows()}
    assert by_id[ids[-1]].outcome == "no_pr"     # the greater id goes first
    assert by_id[ids[0]].outcome == "running"

    await ctx.finalize_outcome("o/r#7", outcome="abandoned", cause="second")
    by_id = {r.id: r for r in await _rows()}
    assert by_id[ids[0]].outcome == "abandoned"
    assert by_id[ids[-1]].outcome == "no_pr"     # untouched by the second call
