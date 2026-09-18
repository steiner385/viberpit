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
async def test_a_second_record_supersedes_the_running_row_it_replaces(db_env):
    """AT MOST ONE running row per (automation, target). A double tap, or a manual
    dispatch over a live claim, used to leave the first row ``running`` forever:
    nothing ever finalised it, so that bounce was invisible to the D8 count and a
    later finalise could stamp the wrong attempt. The new dispatch closes it as
    ``superseded`` — its OWN outcome, so the D8 bounce count ignores it: a double
    tap is not evidence the issue is unworkable (R3-9) — and the sweep then
    finalises the survivor."""
    await _seed(db_env)
    ctx = AutomationContext("backlog-runner")
    await ctx.record_outcome("o/r#7", product_slug="viberpit", session_id="s1",
                             model="sonnet", attempt_no=0)
    await ctx.record_outcome("o/r#7", product_slug="viberpit", session_id="s2",
                             model="opus", attempt_no=1)

    older, newer = await _rows()
    assert older.session_id == "s1" and older.outcome == "superseded"
    # The outcome column carries the whole fact; ``cause`` is not a second copy.
    assert older.cause is None
    assert older.ended_at is not None and older.duration_s is not None
    assert newer.session_id == "s2" and newer.outcome == "running"
    assert newer.ended_at is None

    await ctx.finalize_outcome("o/r#7", outcome="merged", pr_number=42,
                               pr_url="https://example/pr/42", first_pass_merge=True)
    older, newer = await _rows()
    assert newer.attempt_no == 1 and newer.outcome == "merged"
    assert newer.pr_number == 42 and newer.pr_url == "https://example/pr/42"
    assert newer.first_pass_merge is True
    assert older.outcome == "superseded"         # untouched by the finalise
    assert older.pr_number is None


@pytest.mark.asyncio
async def test_finalize_upgrades_an_already_closed_bounce_row(db_env):
    """The runner's give-up branches fire a SWEEP LATER than the bounce they
    escalate: the sweep that charged the last attempt already closed this
    dispatch's row as ``no_pr``, and nothing re-dispatches in between. Without
    ``upgrade_from`` the ``needs_human`` finalise found no running row and was a
    logged no-op, so an escalated issue stayed recorded as a plain bounce (R3-7).

    An upgrade RE-finalises that row in place — one row per dispatch, never a
    second bounce for the same session — and keeps ``ended_at``: the dispatch
    ended when the session did, not when the loop noticed."""
    await _seed(db_env)
    ctx = AutomationContext("backlog-runner")
    await ctx.record_outcome("o/r#7", product_slug="viberpit", session_id="s1",
                             model="sonnet", attempt_no=0)
    await ctx.finalize_outcome("o/r#7", outcome="no_pr",
                               cause="session ended without a mergeable PR")
    (bounced,) = await _rows()
    ended, duration = bounced.ended_at, bounced.duration_s

    await ctx.finalize_outcome("o/r#7", outcome="needs_human",
                               cause="max attempts (2) reached with no mergeable PR",
                               upgrade_from=("no_pr",))
    rows = await _rows()
    assert len(rows) == 1
    r = rows[0]
    assert r.outcome == "needs_human"
    assert r.cause == "max attempts (2) reached with no mergeable PR"
    assert r.ended_at == ended and r.duration_s == duration
    assert r.updated_at is not None and r.session_id == "s1"


@pytest.mark.asyncio
async def test_an_upgrade_never_touches_a_row_outside_upgrade_from(db_env):
    """Only the listed outcomes are upgradable. A merged dispatch is finished
    history: a late escalation with nothing running must leave it alone rather
    than rewrite a landed PR as a needs-human bounce."""
    await _seed(db_env)
    ctx = AutomationContext("backlog-runner")
    await ctx.record_outcome("o/r#7", product_slug="viberpit", session_id=None,
                             model="sonnet", attempt_no=0)
    await ctx.finalize_outcome("o/r#7", outcome="merged", pr_number=42,
                               pr_url="https://example/pr/42", first_pass_merge=True)

    await ctx.finalize_outcome("o/r#7", outcome="needs_human", cause="too late",
                               upgrade_from=("no_pr",))
    rows = await _rows()
    assert len(rows) == 1
    assert rows[0].outcome == "merged" and rows[0].pr_number == 42
    assert rows[0].first_pass_merge is True

    # Nothing running and nothing upgradable for an unknown target: still a no-op.
    await ctx.finalize_outcome("o/r#404", outcome="needs_human", cause="none",
                               upgrade_from=("no_pr",))
    assert len(await _rows()) == 1


@pytest.mark.asyncio
async def test_an_upgrade_prefers_a_running_row_and_takes_the_newest_bounce(db_env):
    """``upgrade_from`` is a FALLBACK: a live dispatch still wins, and when there
    is none the NEWEST upgradable row is the one this give-up is about — an older
    attempt's bounce keeps its own history."""
    await _seed(db_env)
    ctx = AutomationContext("backlog-runner")
    await ctx.record_outcome("o/r#7", product_slug="viberpit", session_id="s1",
                             model="sonnet", attempt_no=0)
    await ctx.finalize_outcome("o/r#7", outcome="no_pr", cause="first bounce")
    await ctx.record_outcome("o/r#7", product_slug="viberpit", session_id="s2",
                             model="sonnet", attempt_no=1)

    # A running row exists → it is finalised, the earlier bounce is untouched.
    await ctx.finalize_outcome("o/r#7", outcome="no_pr", cause="second bounce",
                               upgrade_from=("no_pr",))
    first, second = await _rows()
    assert first.cause == "first bounce" and second.cause == "second bounce"

    # Now nothing runs → the NEWEST bounce is the one that escalates.
    await ctx.finalize_outcome("o/r#7", outcome="needs_human", cause="gave up",
                               upgrade_from=("no_pr",))
    first, second = await _rows()
    assert first.outcome == "no_pr" and first.cause == "first bounce"
    assert second.outcome == "needs_human" and second.cause == "gave up"


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
    current body count, while the ``running`` row, the ``superseded`` row a double
    tap left behind, and a row carrying the pre-edit fingerprint do not."""
    await _seed(db_env)
    ctx = AutomationContext("backlog-runner")
    for outcome in ("no_pr", "pr_gave_up"):
        await ctx.record_outcome("o/r#7", product_slug="viberpit", session_id=None,
                                 model="sonnet", attempt_no=0)
        await ctx.finalize_outcome("o/r#7", outcome=outcome)
    await ctx.record_outcome("o/r#7", product_slug="viberpit", session_id=None,
                             model="sonnet", attempt_no=2)
    # The double tap: this second record supersedes the row above (which is NOT a
    # third bounce) and leaves its own row running.
    await ctx.record_outcome("o/r#7", product_slug="viberpit", session_id=None,
                             model="sonnet", attempt_no=3)
    assert [r.outcome for r in await _rows()][-2:] == ["superseded", "running"]
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
    re-stamping the first.

    ``record_outcome`` now supersedes a running row it can SEE, so the two
    survivors of a genuine race (each insert committed without the other in view)
    are written directly here — the ordering guard has to hold for exactly the
    case the supersede could not prevent."""
    from datetime import datetime, timezone

    await _seed(db_env)
    ctx = AutomationContext("backlog-runner")
    async with db_session() as db:
        for attempt in (0, 1):
            db.add(DispatchOutcome(automation_id="backlog-runner", target_ref="o/r#7",
                                   attempt_no=attempt, outcome="running",
                                   started_at=datetime.now(timezone.utc)))
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


@pytest.mark.asyncio
async def test_finalize_inserts_a_fallback_row_when_the_spawn_write_was_lost(db_env):
    """Spec §13's recovery: "the outcome write fails at spawn: the spawn proceeds;
    the sweep inserts the row at finalisation with ``attempt_no`` from the target
    state". Without it a lost spawn-time write loses that bounce FOREVER — the
    dispatch happened, the issue bounced, and D8 never counts it.

    The inserted row is already finalised: ``started_at`` stays None (nobody knows
    when it began, so ``duration_s`` is unknowable too) while ``ended_at`` is now.
    The fingerprint comes off the mirror row exactly as ``record_outcome`` reads
    it, or the row could never be counted against the body it bounced on."""
    await _seed(db_env)
    ctx = AutomationContext("backlog-runner")
    await ctx.finalize_outcome(
        "o/r#7", outcome="no_pr", cause="session ended without a mergeable PR",
        fallback={"product_slug": "viberpit", "session_id": "s1",
                  "model": "sonnet", "attempt_no": 1})

    (row,) = await _rows()
    assert row.outcome == "no_pr" and row.cause == "session ended without a mergeable PR"
    assert row.body_fingerprint == body_fingerprint("T", "B")
    assert row.project_id is not None
    assert row.session_id == "s1" and row.model == "sonnet" and row.attempt_no == 1
    assert row.started_at is None and row.duration_s is None
    assert row.ended_at is not None and row.updated_at is not None
    assert row.automation_id == "backlog-runner" and row.target_ref == "o/r#7"

    # And D8 counts it: a recovered row is a real bounce against the current body.
    pairs = [("o/r#7", body_fingerprint("T", "B"))]
    async with db_session() as db:
        counted = (await db.execute(bounces_stmt(pairs))).all()
    assert rows_to_bounces(counted, pairs) == {"o/r#7": 1}


@pytest.mark.asyncio
async def test_a_fallback_is_only_a_last_resort(db_env):
    """The fallback NEVER adds a second row: a running row still wins, and so does
    an upgradable one. One row per dispatch is the invariant every reader leans
    on, and a fallback that raced it would double-count the bounce."""
    await _seed(db_env)
    ctx = AutomationContext("backlog-runner")
    fallback = {"product_slug": "viberpit", "session_id": "s9", "model": "opus",
                "attempt_no": 3}

    await ctx.record_outcome("o/r#7", product_slug="viberpit", session_id="s1",
                             model="sonnet", attempt_no=0)
    await ctx.finalize_outcome("o/r#7", outcome="no_pr", cause="bounce",
                               fallback=fallback)
    (row,) = await _rows()
    assert row.session_id == "s1" and row.model == "sonnet"    # the running row won

    await ctx.finalize_outcome("o/r#7", outcome="needs_human", cause="gave up",
                               upgrade_from=("no_pr",), fallback=fallback)
    (row,) = await _rows()
    assert row.outcome == "needs_human" and row.session_id == "s1"


@pytest.mark.asyncio
async def test_a_fallback_never_re_records_a_dispatch_that_already_has_a_row(db_env):
    """One row per DISPATCH, and the dispatch is its session. The sweep re-runs its
    "ended with no PR" branch every tick until the attempt cap, and each of those
    finalises finds nothing to claim — before the fallback they were harmless
    no-ops. Unguarded they would mint a fresh bounce per tick and walk the issue to
    ``needs_spec`` on the strength of one dispatch."""
    await _seed(db_env)
    ctx = AutomationContext("backlog-runner")
    fallback = {"product_slug": "viberpit", "session_id": "s1", "model": "sonnet",
                "attempt_no": 0}
    await ctx.finalize_outcome("o/r#7", outcome="no_pr", cause="bounce",
                               fallback=fallback)
    # The next sweep of the same dead claim: same session, one attempt later.
    await ctx.finalize_outcome("o/r#7", outcome="no_pr", cause="bounce again",
                               fallback={**fallback, "attempt_no": 1})
    (row,) = await _rows()
    assert row.cause == "bounce" and row.attempt_no == 0

    # A LATER dispatch (a new session) whose spawn write was also lost is its own
    # row — the guard is per dispatch, not per target.
    await ctx.finalize_outcome("o/r#7", outcome="no_pr", cause="second dispatch",
                               fallback={**fallback, "session_id": "s2",
                                         "attempt_no": 1})
    first, second = await _rows()
    assert first.session_id == "s1" and second.session_id == "s2"
    assert second.cause == "second dispatch"


@pytest.mark.asyncio
async def test_a_fallback_row_survives_an_unknown_product_and_missing_mirror(db_env):
    """Same tolerance ``record_outcome`` has: an unknown product slug leaves
    ``project_id`` null and an unmirrored ref leaves the fingerprint null (it can
    never be counted against a body nobody has seen). Neither loses the row."""
    await _seed(db_env)
    ctx = AutomationContext("backlog-runner")
    await ctx.finalize_outcome(
        "o/r#999", outcome="no_pr", cause="bounce",
        fallback={"product_slug": "nope", "session_id": None, "model": None,
                  "attempt_no": 0})
    (row,) = await _rows()
    assert row.project_id is None and row.body_fingerprint is None
    assert row.outcome == "no_pr" and row.target_ref == "o/r#999"


@pytest.mark.asyncio
async def test_record_outcome_writes_dispatch_kind_default_implement(db_env):
    """``dispatch_kind`` defaults to ``implement`` when the caller does not pass it
    (spec §3/§5) — an ``investigate`` dispatch must say so explicitly."""
    await _seed(db_env)
    ctx = AutomationContext("backlog-runner")
    await ctx.record_outcome("o/r#1", product_slug=None, session_id="s1",
                             model="sonnet", attempt_no=0)
    await ctx.record_outcome("o/r#2", product_slug=None, session_id="s2",
                             model="sonnet", attempt_no=0,
                             dispatch_kind="investigate")
    rows = {r.target_ref: r for r in await _rows()}
    assert rows["o/r#1"].dispatch_kind == "implement"
    assert rows["o/r#2"].dispatch_kind == "investigate"


@pytest.mark.asyncio
async def test_record_outcome_rejects_an_unknown_dispatch_kind(db_env):
    await _seed(db_env)
    ctx = AutomationContext("backlog-runner")
    with pytest.raises(ValueError):
        await ctx.record_outcome("o/r#1", product_slug=None, session_id="s1",
                                 model="sonnet", attempt_no=0, dispatch_kind="bogus")
