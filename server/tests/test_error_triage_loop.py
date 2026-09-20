from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import datetime, timedelta

from sqlalchemy import select

from server.automations.context import AutomationContext
from server.db import db_session, init_db
from server.db.models import ErrorSignature

NOW = datetime(2026, 9, 7, 12, 0, 0)
CFG = {"target_repo": "o/r", "min_count_error": 1, "min_count_warning": 3,
       "warning_window_h": 24, "daily_cap": 10, "comment_every_h": 24,
       "ignore_loggers": "uvicorn.error,websockets", "retention_days": 90}


class FakeGh:
    """Answers gh calls: label ensure, issue create (incrementing numbers, optional
    failure), comments, and one GraphQL issue-state query."""
    def __init__(self):
        self.calls: list[list[str]] = []
        self.next_number = 42
        self.fail_create = False
        self.states: dict[int, tuple[bool, str | None]] = {}

    def __call__(self, args, *a, **k):
        self.calls.append(list(args))
        path = args[1] if len(args) > 1 else ""
        if path == "graphql":
            data = {f"i{n}": {"number": n, "closed": c, "stateReason": (r or "").upper() or None}
                    for n, (c, r) in self.states.items()}
            return subprocess.CompletedProcess(args, 0, json.dumps({"data": {"repository": data}}), "")
        if path.endswith("/labels"):
            return subprocess.CompletedProcess(args, 0, "{}", "")
        if path.endswith("/comments"):
            return subprocess.CompletedProcess(args, 0, json.dumps({"html_url": "https://x/c"}), "")
        if self.fail_create:
            return subprocess.CompletedProcess(args, 1, "", "boom")
        n, self.next_number = self.next_number, self.next_number + 1
        return subprocess.CompletedProcess(args, 0, json.dumps({"number": n}), "")

    @property
    def creates(self):
        return [c for c in self.calls if c[1] == "repos/o/r/issues"]

    @property
    def comments(self):
        return [c for c in self.calls if c[1].endswith("/comments")]


def _loop(ctx, gh, *, mode="file", config=None, now=NOW):
    from server.automations.error_triage import ErrorTriageLoop
    async def read_config():
        return mode, {**CFG, **(config or {})}
    return ErrorTriageLoop(ctx, read_config=read_config, run=gh, clock=lambda: now)


async def _seed(**kw):
    base = dict(fingerprint="fp1", source="daemon", level="ERROR", logger="server.x",
                title="ERROR server.x: boom", sample_message="boom", count=1,
                count_since_filed=1, status="observed", sample_context={"recent_sessions": []},
                first_seen_at=NOW - timedelta(hours=1), last_seen_at=NOW - timedelta(minutes=1))
    base.update(kw)
    async with db_session() as db:
        db.add(ErrorSignature(**base))
    return base["fingerprint"]


async def _row(fp):
    async with db_session() as db:
        return (await db.execute(select(ErrorSignature).where(
            ErrorSignature.fingerprint == fp))).scalars().one()


def _run(coro_fn, tmp_path):
    async def go():
        await init_db(f"sqlite+aiosqlite:///{tmp_path}/t.db")
        await coro_fn()
    asyncio.run(go())


def test_off_mode_files_nothing(tmp_path):
    async def body():
        await _seed()
        gh = FakeGh()
        await _loop(AutomationContext("error-triage"), gh, mode="off").tick()
        assert gh.calls == [] and (await _row("fp1")).status == "observed"
    _run(body, tmp_path)


def test_error_files_on_first_occurrence_two_phase(tmp_path):
    async def body():
        await _seed(count_since_filed=3, count=3)
        gh = FakeGh()
        ctx = AutomationContext("error-triage")
        await _loop(ctx, gh).tick()
        row = await _row("fp1")
        assert row.status == "filed" and row.issue_number == 42
        assert row.filed_at == NOW and row.count_since_filed == 0
        create = gh.creates[0]
        assert "labels[]=auto-error" in create and "labels[]=source:server" in create
        assert any("title=[auto-error] ERROR server.x: boom" == c for c in create)
        assert ctx.error_triage_counts["filed"] == 1
    _run(body, tmp_path)


def test_warning_threshold_and_stale_burst_reset(tmp_path):
    async def body():
        await _seed(fingerprint="w2", level="WARNING", count_since_filed=2)
        await _seed(fingerprint="w3", level="WARNING", count_since_filed=3)
        await _seed(fingerprint="stale", level="WARNING", count_since_filed=2,
                    last_seen_at=NOW - timedelta(hours=30))
        gh = FakeGh()
        await _loop(AutomationContext("error-triage"), gh).tick()
        assert (await _row("w2")).status == "observed"
        assert (await _row("w3")).status == "filed"
        assert (await _row("stale")).count_since_filed == 0
        assert len(gh.creates) == 1
    _run(body, tmp_path)


def test_ignored_logger_and_invalid_repo(tmp_path):
    async def body():
        await _seed(logger="uvicorn.error")
        gh = FakeGh()
        await _loop(AutomationContext("error-triage"), gh).tick()
        assert gh.creates == []
        await _seed(fingerprint="fp2")
        await _loop(AutomationContext("error-triage"), gh, config={"target_repo": "../x"}).tick()
        assert gh.calls == []
    _run(body, tmp_path)


def test_daily_cap_counts_todays_filings(tmp_path):
    async def body():
        await _seed(fingerprint="a", last_seen_at=NOW - timedelta(minutes=5))
        await _seed(fingerprint="b", last_seen_at=NOW - timedelta(minutes=1))
        await _seed(fingerprint="old", status="filed", issue_number=9,
                    filed_at=NOW - timedelta(days=1, minutes=1), count_since_filed=0)
        gh = FakeGh()
        ctx = AutomationContext("error-triage")
        await _loop(ctx, gh, config={"daily_cap": 1}).tick()
        assert (await _row("b")).status == "filed"      # newest first
        assert (await _row("a")).status == "observed"
        assert ctx.error_triage_counts["candidates"] == 1
        assert ctx.error_triage_counts["filed_today"] == 1
    _run(body, tmp_path)


def test_failed_create_is_retried_next_tick(tmp_path):
    async def body():
        await _seed()
        gh = FakeGh()
        gh.fail_create = True
        loop = _loop(AutomationContext("error-triage"), gh)
        await loop.tick()
        row = await _row("fp1")
        assert row.status == "filed" and row.issue_number is None
        gh.fail_create = False
        await loop.tick()
        row = await _row("fp1")
        assert row.issue_number == 42 and len(gh.creates) == 2
    _run(body, tmp_path)


def test_recurrence_comment_then_throttle(tmp_path):
    async def body():
        await _seed(status="filed", issue_number=42, filed_at=NOW - timedelta(hours=3),
                    count_since_filed=5, count=6)
        gh = FakeGh()
        gh.states = {42: (False, None)}
        loop = _loop(AutomationContext("error-triage"), gh)
        await loop.tick()
        row = await _row("fp1")
        assert len(gh.comments) == 1 and row.count_since_filed == 0
        assert row.last_commented_at == NOW
        assert any("5 more occurrence(s)" in part for part in gh.comments[0])
        async with db_session() as db:
            (await db.execute(select(ErrorSignature))).scalars().one().count_since_filed = 2
        await loop.tick()
        assert len(gh.comments) == 1  # throttled: same clock, <24h since the comment
        assert gh.creates == []
    _run(body, tmp_path)


def test_closed_completed_resolves_then_regression_links_old(tmp_path):
    async def body():
        await _seed(status="filed", issue_number=41, filed_at=NOW - timedelta(days=2),
                    count_since_filed=0)
        gh = FakeGh()
        gh.states = {41: (True, "completed")}
        loop = _loop(AutomationContext("error-triage"), gh)
        await loop.tick()
        row = await _row("fp1")
        assert row.status == "resolved" and row.issue_closed_at == NOW
        assert gh.creates == []
        async with db_session() as db:
            r = (await db.execute(select(ErrorSignature))).scalars().one()
            r.count_since_filed, r.count = 1, 2
        await loop.tick()
        row = await _row("fp1")
        assert row.status == "filed" and row.issue_number == 42
        create = gh.creates[0]
        assert any(c.startswith("title=[auto-error][regression] ") for c in create)
        assert any("| Regression of | #41 |" in c for c in create)
    _run(body, tmp_path)


def test_close_while_still_firing_files_no_preclose_regression(tmp_path):
    """#1878: the pre-close backlog is not evidence that the fix failed.

    #1534 was closed while ``pm_sync.feature_link_unresolved`` was still
    firing, so the row went ``resolved`` still carrying all 520 occurrences
    logged *before* the close. ``should_file`` saw that backlog, ``decide`` saw
    a closed issue, and the tick filed #1878 — a "[regression]" whose every
    occurrence predates #1534's close, for a condition already fixed. The
    sibling caller of the same rule (``deploy_monitor``) has always required a
    *new* sha before re-filing; this is that gate for error triage.

    The seed is the one above with a non-zero counter at close — which is the
    normal case, since a burst loud enough to file is usually still going when
    someone closes the issue.
    """
    async def body():
        await _seed(level="WARNING", status="filed", issue_number=41,
                    filed_at=NOW - timedelta(days=2), count=520, count_since_filed=520)
        gh = FakeGh()
        gh.states = {41: (True, "completed")}
        loop = _loop(AutomationContext("error-triage"), gh)
        await loop.tick()
        row = await _row("fp1")
        assert row.status == "resolved" and row.count_since_filed == 0
        assert gh.creates == [] and gh.comments == []
        await loop.tick()  # and it stays quiet while nothing new arrives
        assert gh.creates == []
        assert (await _row("fp1")).status == "resolved"
    _run(body, tmp_path)


def test_stale_warning_burst_is_reset_for_resolved_rows_too(tmp_path):
    """The burst window has to survive the close, or #1878 comes back slowly.

    ``min_count_warning`` means "3 in ``warning_window_h``", enforced for
    ``observed`` rows by zeroing a burst that has gone quiet. A ``resolved``
    row can file too (as a regression), so post-close occurrences that never
    form a burst must age out the same way — otherwise three stragglers spread
    over three weeks re-file an issue that was correctly closed.
    """
    async def body():
        await _seed(fingerprint="res", level="WARNING", status="resolved", issue_number=41,
                    count_since_filed=2, last_seen_at=NOW - timedelta(hours=30))
        gh = FakeGh()
        await _loop(AutomationContext("error-triage"), gh).tick()
        assert (await _row("res")).count_since_filed == 0
        assert gh.creates == []
    _run(body, tmp_path)


def test_closed_not_planned_mutes_forever(tmp_path):
    async def body():
        await _seed(status="filed", issue_number=42, filed_at=NOW - timedelta(days=2),
                    count_since_filed=7)
        gh = FakeGh()
        gh.states = {42: (True, "not_planned")}
        loop = _loop(AutomationContext("error-triage"), gh)
        await loop.tick()
        row = await _row("fp1")
        assert row.status == "muted" and row.muted_by == "github:not_planned"
        assert gh.comments == [] and gh.creates == []
        await loop.tick()
        assert gh.creates == []
    _run(body, tmp_path)


def test_retention_deletes_stale_unfiled_only(tmp_path):
    async def body():
        old = NOW - timedelta(days=91)
        await _seed(fingerprint="obs", last_seen_at=old, count_since_filed=0)
        await _seed(fingerprint="res", status="resolved", issue_number=1, last_seen_at=old,
                    count_since_filed=0)
        await _seed(fingerprint="fil", status="filed", issue_number=2, last_seen_at=old,
                    count_since_filed=0)
        await _seed(fingerprint="mut", status="muted", last_seen_at=old, count_since_filed=0)
        gh = FakeGh()
        gh.states = {2: (False, None)}
        await _loop(AutomationContext("error-triage"), gh).tick()
        async with db_session() as db:
            left = set((await db.execute(select(ErrorSignature.fingerprint))).scalars().all())
        assert left == {"fil", "mut"}
    _run(body, tmp_path)
