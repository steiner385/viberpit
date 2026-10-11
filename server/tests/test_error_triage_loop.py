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
        # The repo the issue actually landed in, so a later target_repo repoint
        # cannot collide this row's number with a same-numbered one filed under
        # the new target (#1963).
        assert row.repo == "o/r"
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
        # Distinct titles: same-titled rows would collapse into one issue (#2683).
        await _seed(fingerprint="a", title="ERROR server.x: a", last_seen_at=NOW - timedelta(minutes=5))
        await _seed(fingerprint="b", title="ERROR server.x: b", last_seen_at=NOW - timedelta(minutes=1))
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


def test_stale_warning_burst_is_reset_for_muted_rows_too(tmp_path):
    """errsink.py keeps incrementing count_since_filed on every flush regardless
    of status, including muted rows (#2104, sibling of #1878). Without muted in
    this reset's status set, a muted WARNING burst never ages out — it just
    grows, ready to replay in full the moment someone unmutes it."""
    async def body():
        await _seed(fingerprint="mut", level="WARNING", status="muted",
                    muted_by="operator", count_since_filed=2,
                    last_seen_at=NOW - timedelta(hours=30))
        gh = FakeGh()
        await _loop(AutomationContext("error-triage"), gh).tick()
        assert (await _row("mut")).count_since_filed == 0
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


# -- duplicate collapse (#2683) -------------------------------------------------
# #2776/#2777/#2784/#2787: one Postgres outage, one rendered title, four
# fingerprints (frame/source differ) → four issues. #2727/#2728/#2729: one
# browser tab, three messages within a minute → three issues.

REFUSED = "ERROR server: unhandled exception: [errno <n>] connection refused"


def test_same_title_links_to_earliest_open_issue_without_creating(tmp_path):
    async def body():
        await _seed(fingerprint="old60", title=REFUSED, status="filed", issue_number=60,
                    repo="o/r", filed_at=NOW - timedelta(days=1), count_since_filed=0)
        await _seed(fingerprint="old50", title=REFUSED, status="filed", issue_number=50,
                    repo="o/r", filed_at=NOW - timedelta(days=2), count_since_filed=0)
        await _seed(fingerprint="new", title=REFUSED, source="edge", count_since_filed=4, count=4)
        gh = FakeGh()
        gh.states = {50: (False, None), 60: (False, None)}
        ctx = AutomationContext("error-triage")
        await _loop(ctx, gh).tick()
        assert gh.creates == []
        row = await _row("new")
        assert row.status == "filed" and row.issue_number == 50 and row.repo == "o/r"
        assert row.filed_at is None                 # a link is not a filing: no cap spent
        assert row.count_since_filed == 0 and row.last_commented_at == NOW
        assert len(gh.comments) == 1 and gh.comments[0][1].endswith("/issues/50/comments")
        assert any("`new`" in part for part in gh.comments[0])
        assert ctx.error_triage_counts["filed_today"] == 0
    _run(body, tmp_path)


def test_same_title_link_respects_the_issue_comment_throttle(tmp_path):
    async def body():
        await _seed(fingerprint="old", title=REFUSED, status="filed", issue_number=50,
                    repo="o/r", filed_at=NOW - timedelta(days=2), count_since_filed=0,
                    last_commented_at=NOW - timedelta(hours=1))
        await _seed(fingerprint="new", title=REFUSED, count_since_filed=4)
        gh = FakeGh()
        gh.states = {50: (False, None)}
        await _loop(AutomationContext("error-triage"), gh).tick()
        row = await _row("new")
        assert row.status == "filed" and row.issue_number == 50
        assert gh.creates == [] and gh.comments == []
        # Occurrences stay counted: the recurrence comment reports them once the
        # issue's throttle lapses, on the issue's clock — not a fresh one.
        assert row.count_since_filed == 4
        assert row.last_commented_at == NOW - timedelta(hours=1)
    _run(body, tmp_path)


def test_closed_earlier_issue_does_not_absorb(tmp_path):
    async def body():
        await _seed(fingerprint="old", title=REFUSED, status="filed", issue_number=50,
                    repo="o/r", filed_at=NOW - timedelta(days=2), count_since_filed=0)
        await _seed(fingerprint="new", title=REFUSED, count_since_filed=1)
        gh = FakeGh()
        gh.states = {50: (True, "completed")}
        await _loop(AutomationContext("error-triage"), gh).tick()
        assert len(gh.creates) == 1 and gh.comments == []
        assert (await _row("new")).issue_number == 42
        assert (await _row("old")).status == "resolved"
    _run(body, tmp_path)


def test_muted_sibling_does_not_absorb_and_stays_muted(tmp_path):
    async def body():
        await _seed(fingerprint="old", title=REFUSED, status="muted", issue_number=50,
                    muted_by="github:not_planned", count_since_filed=9)
        await _seed(fingerprint="new", title=REFUSED, count_since_filed=1)
        gh = FakeGh()
        await _loop(AutomationContext("error-triage"), gh).tick()
        assert len(gh.creates) == 1 and gh.comments == []
        assert (await _row("old")).status == "muted"
    _run(body, tmp_path)


def test_regression_stays_a_regression_beside_an_open_sibling(tmp_path):
    """A fingerprint whose own issue was closed as fixed and then recurred is
    evidence the fix failed: it keeps the regress path even when a same-titled
    sibling is still open."""
    async def body():
        await _seed(fingerprint="open", title=REFUSED, status="filed", issue_number=50,
                    repo="o/r", count_since_filed=0)
        await _seed(fingerprint="reg", title=REFUSED, status="resolved", issue_number=41,
                    count_since_filed=1)
        gh = FakeGh()
        gh.states = {50: (False, None)}
        await _loop(AutomationContext("error-triage"), gh).tick()
        assert len(gh.creates) == 1
        assert any(c.startswith("title=[auto-error][regression] ") for c in gh.creates[0])
        assert any("| Regression of | #41 |" in c for c in gh.creates[0])
        assert (await _row("reg")).issue_number == 42
    _run(body, tmp_path)


def test_same_title_fingerprints_in_one_pass_file_once(tmp_path):
    async def body():
        for i, src in enumerate(("edge", "daemon", "edge", "daemon")):
            await _seed(fingerprint=f"fp{i}", title=REFUSED, source=src,
                        first_seen_at=NOW - timedelta(minutes=10 - i), count_since_filed=2)
        gh = FakeGh()
        ctx = AutomationContext("error-triage")
        await _loop(ctx, gh).tick()
        assert len(gh.creates) == 1
        body_arg = next(c for c in gh.creates[0] if c.startswith("body="))
        assert all(f"`fp{i}`" in body_arg for i in range(4))
        rows = [await _row(f"fp{i}") for i in range(4)]
        assert {r.issue_number for r in rows} == {42}
        assert {r.status for r in rows} == {"filed"}
        assert [r.count_since_filed for r in rows] == [0, 0, 0, 0]
        assert rows[0].filed_at == NOW                        # earliest leads
        assert [r.filed_at for r in rows[1:]] == [None, None, None]
        assert ctx.error_triage_counts["filed_today"] == 1
    _run(body, tmp_path)


def _client(fp, title, *, seen, client_id="tab-1"):
    return _seed(fingerprint=fp, title=title, source="client", logger="client",
                 event="client.error", first_seen_at=seen, last_seen_at=seen,
                 sample_context={"recent_sessions": [], "client_id": client_id})


def test_client_burst_within_window_files_one_issue(tmp_path):
    async def body():
        t0 = NOW - timedelta(minutes=5)
        await _client("c2", "ERROR client: failed to fetch", seen=t0 + timedelta(seconds=20))
        await _client("c1", "ERROR client: api error <n>", seen=t0)
        await _client("c3", "ERROR client: mutation: something went wrong",
                      seen=t0 + timedelta(seconds=50))
        gh = FakeGh()
        await _loop(AutomationContext("error-triage"), gh).tick()
        assert len(gh.creates) == 1
        assert any(c == "title=[auto-error] ERROR client: api error <n>" for c in gh.creates[0])
        assert {(await _row(f)).issue_number for f in ("c1", "c2", "c3")} == {42}
    _run(body, tmp_path)


def test_client_errors_outside_window_or_other_tab_file_separately(tmp_path):
    async def body():
        t0 = NOW - timedelta(minutes=30)
        await _client("c1", "ERROR client: api error <n>", seen=t0)
        await _client("late", "ERROR client: failed to fetch", seen=t0 + timedelta(minutes=10))
        await _client("tab2", "ERROR client: mutation failed", seen=t0 + timedelta(seconds=5),
                      client_id="tab-2")
        gh = FakeGh()
        await _loop(AutomationContext("error-triage"), gh).tick()
        assert len(gh.creates) == 3
    _run(body, tmp_path)


def test_client_burst_straddling_ticks_joins_the_open_issue(tmp_path):
    """The tick runs every 60 s, so a burst can split across two passes: the
    second half links to the issue the first half opened."""
    async def body():
        t0 = NOW - timedelta(minutes=2)
        await _client("c1", "ERROR client: api error <n>", seen=t0)
        await _seed(fingerprint="c1x", title="x")  # unrelated, files on its own
        async with db_session() as db:
            r = (await db.execute(select(ErrorSignature).where(
                ErrorSignature.fingerprint == "c1"))).scalars().one()
            r.status, r.issue_number, r.repo, r.count_since_filed = "filed", 50, "o/r", 0
        await _client("c2", "ERROR client: failed to fetch", seen=t0 + timedelta(seconds=40))
        gh = FakeGh()
        gh.states = {50: (False, None)}
        await _loop(AutomationContext("error-triage"), gh).tick()
        assert (await _row("c2")).issue_number == 50
        assert len(gh.creates) == 1                     # only the unrelated "x"
        assert any("`c2`" in part for part in gh.comments[0])
    _run(body, tmp_path)


def test_collapse_costs_no_cap(tmp_path):
    async def body():
        # Today's cap is already spent…
        await _seed(fingerprint="today", title="ERROR other", status="filed", issue_number=9,
                    repo="o/r", filed_at=NOW - timedelta(hours=1), count_since_filed=0)
        await _seed(fingerprint="open", title=REFUSED, status="filed", issue_number=50,
                    repo="o/r", filed_at=NOW - timedelta(days=3), count_since_filed=0)
        # …yet a same-titled newcomer still links, and a brand-new title waits.
        await _seed(fingerprint="new", title=REFUSED, count_since_filed=1)
        await _seed(fingerprint="fresh", title="ERROR brand new", count_since_filed=1)
        gh = FakeGh()
        gh.states = {9: (False, None), 50: (False, None)}
        ctx = AutomationContext("error-triage")
        await _loop(ctx, gh, config={"daily_cap": 1}).tick()
        assert (await _row("new")).issue_number == 50
        assert (await _row("fresh")).status == "observed"
        assert gh.creates == []
        assert ctx.error_triage_counts["filed_today"] == 1
        assert ctx.error_triage_counts["candidates"] == 1
    _run(body, tmp_path)


def test_fixed_close_retires_each_linked_rows_own_backlog(tmp_path):
    """Rows sharing an issue carry different counts: each drops its OWN
    pre-close backlog. Subtracting one row's count from all either leaves a
    stale positive (a spurious regression this very tick) or drives a row
    negative (a real regression hidden forever)."""
    async def body():
        await _seed(fingerprint="a", title=REFUSED, status="filed", issue_number=50,
                    repo="o/r", count_since_filed=0)
        await _seed(fingerprint="b", title=REFUSED, status="filed", issue_number=50,
                    repo="o/r", count_since_filed=5)
        await _seed(fingerprint="c", title=REFUSED, status="filed", issue_number=50,
                    repo="o/r", count_since_filed=2)
        gh = FakeGh()
        gh.states = {50: (True, "completed")}
        await _loop(AutomationContext("error-triage"), gh).tick()
        rows = [await _row(f) for f in ("a", "b", "c")]
        assert [r.status for r in rows] == ["resolved"] * 3
        assert [r.count_since_filed for r in rows] == [0, 0, 0]
        assert gh.creates == []
    _run(body, tmp_path)


def test_rows_of_one_closed_issue_regress_as_one(tmp_path):
    async def body():
        await _seed(fingerprint="a", title=REFUSED, status="resolved", issue_number=50,
                    count_since_filed=1, first_seen_at=NOW - timedelta(hours=3))
        await _seed(fingerprint="b", title=REFUSED, status="resolved", issue_number=50,
                    count_since_filed=2, first_seen_at=NOW - timedelta(hours=2))
        # Same title but a different closed issue: its own regression lineage.
        await _seed(fingerprint="z", title=REFUSED, status="resolved", issue_number=60,
                    count_since_filed=1)
        gh = FakeGh()
        await _loop(AutomationContext("error-triage"), gh).tick()
        assert len(gh.creates) == 2
        a, b, z = (await _row("a")), (await _row("b")), (await _row("z"))
        assert a.issue_number == b.issue_number and a.issue_number != z.issue_number
        assert a.count_since_filed == b.count_since_filed == 0
        reg50 = next(c for c in gh.creates if any("| Regression of | #50 |" in p for p in c))
        assert (await _row("a")).filed_at == NOW and b.filed_at is None  # earliest leads
        assert any("`b`" in p for p in reg50)
    _run(body, tmp_path)


def test_recurrence_comment_is_one_per_issue_aggregating_linked_rows(tmp_path):
    async def body():
        await _seed(fingerprint="a", title=REFUSED, status="filed", issue_number=50,
                    repo="o/r", filed_at=NOW - timedelta(days=2), count_since_filed=3)
        await _seed(fingerprint="b", title=REFUSED, status="filed", issue_number=50,
                    repo="o/r", filed_at=NOW - timedelta(days=2), count_since_filed=4)
        # The issue was commented on recently through a THIRD row: throttled.
        await _seed(fingerprint="c", title=REFUSED, status="filed", issue_number=60,
                    repo="o/r", count_since_filed=0)
        await _seed(fingerprint="d", title="ERROR other", status="filed", issue_number=60,
                    repo="o/r", count_since_filed=5,
                    last_commented_at=None, filed_at=NOW - timedelta(days=2))
        await _seed(fingerprint="e", title="ERROR other2", status="filed", issue_number=60,
                    repo="o/r", count_since_filed=0, last_commented_at=NOW - timedelta(hours=1))
        gh = FakeGh()
        gh.states = {50: (False, None), 60: (False, None)}
        loop = _loop(AutomationContext("error-triage"), gh)
        await loop.tick()
        assert len(gh.comments) == 1
        assert gh.comments[0][1].endswith("/issues/50/comments")
        text = " ".join(gh.comments[0])
        assert "7 more occurrence(s)" in text and "`a`" in text and "`b`" in text
        assert (await _row("a")).count_since_filed == 0
        assert (await _row("b")).count_since_filed == 0
        assert (await _row("d")).count_since_filed == 5     # held by #60's throttle
        await loop.tick()
        assert len(gh.comments) == 1
    _run(body, tmp_path)


def test_failed_regression_leader_retries_as_one_regression(tmp_path):
    async def body():
        await _seed(fingerprint="a", title=REFUSED, status="resolved", issue_number=50,
                    count_since_filed=1, first_seen_at=NOW - timedelta(hours=2))
        await _seed(fingerprint="b", title=REFUSED, status="resolved", issue_number=50,
                    count_since_filed=1, first_seen_at=NOW - timedelta(hours=1))
        gh = FakeGh()
        gh.fail_create = True
        loop = _loop(AutomationContext("error-triage"), gh)
        await loop.tick()
        assert len(gh.creates) == 1
        assert (await _row("b")).status == "resolved"   # not claimed: no orphan retry
        gh.fail_create = False
        await loop.tick()
        assert len(gh.creates) == 2                      # the leader's retry, nothing else
        retry = gh.creates[1]
        assert any(c.startswith("title=[auto-error][regression] ") for c in retry)
        assert any("| Regression of | #50 |" in c for c in retry)
        assert any("`b`" in c for c in retry)
        assert {(await _row(f)).issue_number for f in ("a", "b")} == {42}
    _run(body, tmp_path)


def test_failed_leader_keeps_followers_waiting_then_links_them(tmp_path):
    async def body():
        await _seed(fingerprint="a", title=REFUSED, first_seen_at=NOW - timedelta(hours=2))
        await _seed(fingerprint="b", title=REFUSED, first_seen_at=NOW - timedelta(hours=1))
        gh = FakeGh()
        gh.fail_create = True
        loop = _loop(AutomationContext("error-triage"), gh)
        await loop.tick()
        assert len(gh.creates) == 1
        assert (await _row("b")).status == "observed"   # not claimed: no orphan retry
        gh.fail_create = False
        await loop.tick()
        assert len(gh.creates) == 2                      # the leader's retry, nothing else
        assert {(await _row(f)).issue_number for f in ("a", "b")} == {42}
    _run(body, tmp_path)
