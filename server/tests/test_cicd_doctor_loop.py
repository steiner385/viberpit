"""CicdDoctorLoop — driven entirely through fakes (no network)."""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess

from sqlalchemy import select

from server.automations.cicd_doctor import CicdDoctorLoop
from server.automations.context import AutomationContext
from server.db import db_session, init_db
from server.db.models import CiFinding, CiTestResult

JUNIT_FLAKY = b"""<testsuite><testcase classname="t" name="flaky" time="1">
<failure message="boom"/></testcase></testsuite>"""
JUNIT_PASS = b'<testsuite><testcase classname="t" name="flaky" time="1"/></testsuite>'
JUNIT_MALFORMED = b"<testsuite><testcase"


def _runs(n, sha="s1", ts=1000.0):
    return [{"id": i, "workflow_path": "wf.yml", "event": "pull_request",
             "head_sha": sha, "head_branch": "feat", "conclusion": "success",
             "created_ts": ts + i, "duration_s": 60.0} for i in range(1, n + 1)]


class FakeGh:
    """Answers the doctor's gh calls: label ensure, issue create (incrementing
    numbers), comments, and the close PATCH (optionally failing)."""

    def __init__(self, *, close_ok=None):
        self.calls: list[list[str]] = []
        self.next_number = 42
        self._close_ok = close_ok if close_ok is not None else (lambda: True)

    def __call__(self, args, *a, **k):
        self.calls.append(list(args))
        if "-X" in args and "PATCH" in args:                   # close
            return subprocess.CompletedProcess(
                args, 0 if self._close_ok() else 1, "{}", "")
        path = args[1] if len(args) > 1 else ""
        if path.endswith("/labels") or path.endswith("/comments"):
            return subprocess.CompletedProcess(
                args, 0, json.dumps({"html_url": "https://x/c"}), "")
        n, self.next_number = self.next_number, self.next_number + 1
        return subprocess.CompletedProcess(args, 0, json.dumps({"number": n}), "")

    @property
    def created(self) -> list[str]:
        """Titles of issues actually created, in order."""
        return [a[a.index("-f") + 1][len("title="):] for a in self.calls
                if len(a) > 1 and a[1].endswith("/issues")]

    @property
    def created_bodies(self) -> list[str]:
        out = []
        for a in self.calls:
            if len(a) > 1 and a[1].endswith("/issues"):
                out.append(next(v[len("body="):] for v in a if v.startswith("body=")))
        return out

    @property
    def comments(self) -> list[str]:
        return [next(v[len("body="):] for v in a if v.startswith("body="))
                for a in self.calls if len(a) > 1 and a[1].endswith("/comments")]

    @property
    def closed(self) -> list[int]:
        return [int(a[a.index("-X") + 2].rsplit("/", 1)[1]) for a in self.calls
                if "-X" in a and "PATCH" in a]


def _loop(ctx, *, runs, junit_by_run, mode="propose", config=None, gh=None,
          repos=("o/r",)):
    gh = gh if gh is not None else FakeGh()
    repos = list(repos)
    async def read_config():
        return mode, {"enabled_repos": [], "file_issues": True, "min_window_days": 21,
                      "min_runs": 30, "max_flake_pct": 2, "max_p95_minutes": 10,
                      "demote_flake_pct": 5, "demote_window_days": 7, "retire_days": 60,
                      "flaky_test_min_pct": 2, "retention_days": 90, **(config or {})}
    async def fetch_junit(repo, run_id):
        return junit_by_run.get(run_id, [])
    return CicdDoctorLoop(
        ctx,
        read_config=read_config,
        list_repos=lambda config: _async(repos),
        fetch_runs=lambda repo, since: _async([r for r in runs
                                               if not since or r["created_ts"] > since]),
        fetch_jobs=lambda repo, rid: _async([{"id": rid * 10, "name": "e2e",
                                              "conclusion": "success", "duration_s": 60.0}]),
        fetch_junit=fetch_junit,
        fetch_repo_meta=lambda repo: _async(("master", {"lint-and-test"})),
        fetch_workflows=lambda repo, br: _async({}),
        run=gh,
        clock=lambda: 2000.0,
    )


async def _async(v):
    return v


def test_mode_off_ingests_nothing(tmp_path):
    async def run():
        await init_db(f"sqlite+aiosqlite:///{tmp_path}/t.db")
        ctx = AutomationContext("cicd-doctor")
        loop = _loop(ctx, runs=_runs(2), junit_by_run={}, mode="off")
        await loop.tick()
        async with db_session() as db:
            assert (await db.execute(select(CiTestResult))).scalars().all() == []
        # Cursor must not be advanced when mode is off.
        assert await ctx.get_target_state("cursor:o/r") == {}
    asyncio.run(run())


def test_ingest_is_cursor_idempotent(tmp_path):
    async def run():
        await init_db(f"sqlite+aiosqlite:///{tmp_path}/t.db")
        ctx = AutomationContext("cicd-doctor")
        loop = _loop(ctx, runs=_runs(2),
                     junit_by_run={1: [("junit-python", JUNIT_PASS)]})
        await loop.tick()
        await loop.tick()  # second tick: cursor skips both runs
        async with db_session() as db:
            rows = (await db.execute(select(CiTestResult))).scalars().all()
            assert len(rows) == 1 and rows[0].test_id == "t::flaky"
        cursor = await ctx.get_target_state("cursor:o/r")
        assert cursor["last_created_ts"] == 1002.0
    asyncio.run(run())


def test_malformed_junit_warnings_are_throttled_per_repo(tmp_path, caplog):
    """#1704: a repo whose CI keeps producing bad junit XML re-entered the
    per-run warning on every ingested run — 145 records for one standing
    condition. Throttled on the repo, it must report once per tick."""
    async def run():
        await init_db(f"sqlite+aiosqlite:///{tmp_path}/t.db")
        ctx = AutomationContext("cicd-doctor")
        loop = _loop(ctx, runs=_runs(3),
                     junit_by_run={i: [("junit-python", JUNIT_MALFORMED)]
                                   for i in (1, 2, 3)})
        with caplog.at_level(logging.WARNING, logger="server.automations.cicd_doctor"):
            await loop.tick()
    asyncio.run(run())
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1, (
        f"{len(warnings)} warnings for one standing malformed-junit condition")
    assert warnings[0].fields == {"repo": "o/r", "run": 1, "count": 1}


def test_flaky_test_becomes_finding_and_issue(tmp_path):
    async def run():
        await init_db(f"sqlite+aiosqlite:///{tmp_path}/t.db")
        ctx = AutomationContext("cicd-doctor")
        gh = FakeGh()
        # Evidence volume matters: flaky_test_findings needs >=3 distinct shas.
        # sha s1 both fails (run 1) and passes (run 2); s2 and s3 are clean
        # -> 33.3% flaky across 3 shas, above the 2% threshold.
        runs = [
            {"id": 1, "workflow_path": "wf.yml", "event": "pull_request",
             "head_sha": "s1", "head_branch": "feat", "conclusion": "success",
             "created_ts": 1001.0, "duration_s": 60.0},
            {"id": 2, "workflow_path": "wf.yml", "event": "pull_request",
             "head_sha": "s1", "head_branch": "feat", "conclusion": "success",
             "created_ts": 1002.0, "duration_s": 60.0},
            {"id": 3, "workflow_path": "wf.yml", "event": "pull_request",
             "head_sha": "s2", "head_branch": "feat", "conclusion": "success",
             "created_ts": 1003.0, "duration_s": 60.0},
            {"id": 4, "workflow_path": "wf.yml", "event": "pull_request",
             "head_sha": "s3", "head_branch": "feat", "conclusion": "success",
             "created_ts": 1004.0, "duration_s": 60.0},
        ]
        loop = _loop(ctx, runs=runs,
                     junit_by_run={1: [("junit-python", JUNIT_FLAKY)],
                                   2: [("junit-python", JUNIT_PASS)],
                                   3: [("junit-python", JUNIT_PASS)],
                                   4: [("junit-python", JUNIT_PASS)]},
                     gh=gh)
        await loop.tick()
        async with db_session() as db:
            f = (await db.execute(select(CiFinding).where(
                CiFinding.kind == "flaky_test"))).scalars().one()
            assert f.status == "open" and f.issue_number == 42
            # #450: the parsed failure message rides through ingest -> finding.
            assert (f.detail or {}).get("message") == "boom"
        assert any("flaky" in t for t in gh.created)
        # #1433: the ci-doctor label survived the move onto issue_filing.apply,
        # which (unlike create_issue's default) is passed labels explicitly.
        assert any("labels[]=ci-doctor" in " ".join(c) for c in gh.calls)
        assert ctx.cicd_doctor_counts["open"] >= 1
    asyncio.run(run())


def test_detected_event_lands_outside_db_session(tmp_path):
    """Regression for I1: emit (which opens its own db_session) was called while
    the reconcile db_session still held the SQLite write lock, causing the INSERT
    to hit the busy_timeout and be silently swallowed.  After the fix, reconcile
    side effects (emit, file_issue, close_issue) run after the session exits, so
    the event always lands."""
    async def run():
        await init_db(f"sqlite+aiosqlite:///{tmp_path}/t.db")
        ctx = AutomationContext("cicd-doctor")
        runs = [
            {"id": 1, "workflow_path": "wf.yml", "event": "pull_request",
             "head_sha": "s1", "head_branch": "feat", "conclusion": "success",
             "created_ts": 1001.0, "duration_s": 60.0},
            {"id": 2, "workflow_path": "wf.yml", "event": "pull_request",
             "head_sha": "s1", "head_branch": "feat", "conclusion": "success",
             "created_ts": 1002.0, "duration_s": 60.0},
            {"id": 3, "workflow_path": "wf.yml", "event": "pull_request",
             "head_sha": "s2", "head_branch": "feat", "conclusion": "success",
             "created_ts": 1003.0, "duration_s": 60.0},
            {"id": 4, "workflow_path": "wf.yml", "event": "pull_request",
             "head_sha": "s3", "head_branch": "feat", "conclusion": "success",
             "created_ts": 1004.0, "duration_s": 60.0},
        ]
        loop = _loop(ctx, runs=runs,
                     junit_by_run={1: [("junit-python", JUNIT_FLAKY)],
                                   2: [("junit-python", JUNIT_PASS)],
                                   3: [("junit-python", JUNIT_PASS)],
                                   4: [("junit-python", JUNIT_PASS)]})
        await loop.tick()
        # The "detected" event must actually land in the DB — on the old code it
        # was swallowed by the busy_timeout because emit opened a nested write
        # session while reconcile's session still held the lock.
        events = await ctx.list_events(kind="detected")
        assert events, "detected event was not emitted (emit ran inside the write lock)"
    asyncio.run(run())


def test_analyze_windows_out_stale_history(tmp_path):
    """#449: _analyze must ignore rows older than the analysis horizon. A run
    outside the window doesn't count toward the "runs but no JUnit" verdict; one
    inside it does. Proven via the windowed run_count → missing_junit finding."""
    async def run():
        await init_db(f"sqlite+aiosqlite:///{tmp_path}/t.db")
        from server.db.models import CiRun
        ctx = AutomationContext("cicd-doctor")
        # Collapse every window to 1 day so horizon == 1 day (86400s).
        cfg = {"retention_days": 1, "retire_days": 1, "min_window_days": 1,
               "demote_window_days": 1}
        loop = _loop(ctx, runs=[], junit_by_run={}, config=cfg)  # clock() == 2000.0
        config = (await loop._read_config())[1]

        async def seed(created_ts):
            async with db_session() as db:
                db.add(CiRun(id=int(created_ts), repo="o/r", workflow_path="wf.yml",
                             event="pull_request", head_sha="s", head_branch="feat",
                             conclusion="success", created_ts=created_ts, duration_s=1.0))

        # A run created well before now - horizon (2000 - 86400 < 0, so anything
        # with a tiny ts is inside; use a large negative-relative gap): seed one
        # far in the past and one "now".
        await seed(1.0)          # far outside the 1-day window (age ~2000s vs 86400s? inside!)
        fresh = await loop._analyze("o/r", config)
        # 2000 - 86400 is negative, so ts=1.0 is INSIDE the window → run counts →
        # missing_junit finding present (there are runs, no test rows).
        assert any(f.kind == "missing_junit" for f in fresh)

        # Now widen the gap: a run at ts=1.0 with clock far in the future is OUTSIDE.
        loop._clock = lambda: 1_000_000.0
        fresh2 = await loop._analyze("o/r", config)
        assert not any(f.kind == "missing_junit" for f in fresh2), \
            "stale run outside the horizon must not count toward analysis"
    asyncio.run(run())


def _finding():
    from server.automations.cicd.types import Finding
    return Finding(kind="flaky_test", repo="o/r", key="t::a",
                   title="flaky test: t::a", detail={"flake_pct": 8.0})


def test_flap_after_confirmed_close_files_a_linked_regression(tmp_path):
    """#1433 (replaces #448's reopen): a finding that resolves — closing its issue
    — and then recurs files a NEW issue linking the closed one, rather than
    reopening it. This is issue_filing.decide()'s shared rule, the same one error
    triage and the session-feedback filer follow: a finding that comes back after
    its fix is news, not a continuation of the old thread."""
    async def run():
        await init_db(f"sqlite+aiosqlite:///{tmp_path}/t.db")
        ctx = AutomationContext("cicd-doctor")
        gh = FakeGh()
        loop = _loop(ctx, runs=[], junit_by_run={}, gh=gh)
        cfg = {"file_issues": True, "retention_days": 90}
        f = _finding()

        await loop._reconcile("o/r", [f], cfg)      # detect -> file issue 42
        assert gh.created == ["flaky test: t::a"]
        async with db_session() as db:
            row = (await db.execute(select(CiFinding))).scalars().one()
            assert row.status == "open" and row.issue_number == 42

        await loop._reconcile("o/r", [], cfg)        # resolve -> close issue 42
        assert gh.closed == [42]
        async with db_session() as db:
            row = (await db.execute(select(CiFinding))).scalars().one()
            assert row.status == "resolved" and row.issue_closed is True

        await loop._reconcile("o/r", [f], cfg)       # recur -> NEW linked issue
        assert gh.created == ["flaky test: t::a", "flaky test: t::a"]
        assert "Recurrence of #42" in gh.created_bodies[1]
        assert gh.comments == []                     # never a comment on a closed issue
        assert gh.closed == [42]                     # and never reopened
        async with db_session() as db:
            rows = (await db.execute(select(CiFinding).order_by(
                CiFinding.issue_number))).scalars().all()
            # The resolved row keeps its history; the recurrence is its own row.
            assert [(r.status, r.issue_number, r.issue_closed) for r in rows] == [
                ("resolved", 42, True), ("open", 43, None)]
    asyncio.run(run())


def test_recurrence_before_the_close_confirms_comments_instead_of_filing(tmp_path):
    """The other half of the #1433 rule: when the close never confirmed, the
    original issue is still open on GitHub, so decide() says "comment". Filing a
    second issue for one that is already open would be the duplicate #448 set out
    to prevent."""
    async def run():
        await init_db(f"sqlite+aiosqlite:///{tmp_path}/t.db")
        ctx = AutomationContext("cicd-doctor")
        ok = {"v": False}
        gh = FakeGh(close_ok=lambda: ok["v"])
        loop = _loop(ctx, runs=[], junit_by_run={}, gh=gh)
        cfg = {"file_issues": True, "retention_days": 90}
        f = _finding()

        await loop._reconcile("o/r", [f], cfg)       # file issue 42
        await loop._reconcile("o/r", [], cfg)        # resolve; close FAILS
        async with db_session() as db:
            row = (await db.execute(select(CiFinding))).scalars().one()
            assert row.status == "resolved" and row.issue_closed is None

        await loop._reconcile("o/r", [f], cfg)       # recur while #42 is still open
        assert gh.created == ["flaky test: t::a"]    # no second issue
        assert len(gh.comments) == 1 and "Recurred before this issue was closed" in gh.comments[0]
        async with db_session() as db:
            rows = (await db.execute(select(CiFinding))).scalars().all()
            assert len(rows) == 1                    # same row revived, not a new one
            assert rows[0].status == "open" and rows[0].issue_number == 42

        # And the comment fires once per flap, not once per tick: the row is open
        # now, so a tick that still sees the finding takes the update branch.
        await loop._reconcile("o/r", [f], cfg)
        assert len(gh.comments) == 1
    asyncio.run(run())


def test_failed_close_is_retried_next_tick(tmp_path):
    """#448: a close that fails leaves the issue open but is retried, not lost."""
    async def run():
        await init_db(f"sqlite+aiosqlite:///{tmp_path}/t.db")
        ctx = AutomationContext("cicd-doctor")
        ok = {"v": False}
        gh = FakeGh(close_ok=lambda: ok["v"])
        loop = _loop(ctx, runs=[], junit_by_run={}, gh=gh)
        cfg = {"file_issues": True, "retention_days": 90}
        f = _finding()

        await loop._reconcile("o/r", [f], cfg)       # file issue 42
        await loop._reconcile("o/r", [], cfg)         # resolve; close FAILS
        assert gh.closed == [42]
        async with db_session() as db:
            row = (await db.execute(select(CiFinding))).scalars().one()
            assert row.status == "resolved" and row.issue_closed is None  # not confirmed

        ok["v"] = True
        await loop._reconcile("o/r", [], cfg)         # retry sweep closes it
        assert gh.closed == [42, 42]                  # retried
        async with db_session() as db:
            row = (await db.execute(select(CiFinding))).scalars().one()
            assert row.issue_closed is True
    asyncio.run(run())


def test_open_row_with_no_issue_retries_the_file(tmp_path):
    """A create that failed leaves the open row's issue_number NULL; decide()
    calls that "no linked issue -> file", so the next tick retries it."""
    async def run():
        await init_db(f"sqlite+aiosqlite:///{tmp_path}/t.db")
        ctx = AutomationContext("cicd-doctor")
        gh = FakeGh()
        loop = _loop(ctx, runs=[], junit_by_run={}, gh=gh)
        cfg = {"file_issues": True, "retention_days": 90}
        f = _finding()

        await loop._reconcile("o/r", [f], cfg)
        async with db_session() as db:   # simulate the create having failed
            from sqlalchemy import update as sa_update
            await db.execute(sa_update(CiFinding).values(issue_number=None))

        await loop._reconcile("o/r", [f], cfg)
        assert len(gh.created) == 2
        async with db_session() as db:
            rows = (await db.execute(select(CiFinding))).scalars().all()
            assert len(rows) == 1 and rows[0].issue_number == 43
    asyncio.run(run())


def test_prune_drops_old_resolved_findings_keeps_open(tmp_path):
    """#448: resolved findings past retention are pruned; open ones never are."""
    async def run():
        from datetime import timedelta
        from server.db.models import _utcnow
        await init_db(f"sqlite+aiosqlite:///{tmp_path}/t.db")
        ctx = AutomationContext("cicd-doctor")
        loop = _loop(ctx, runs=[], junit_by_run={})
        old = _utcnow() - timedelta(days=200)
        recent = _utcnow() - timedelta(days=1)
        async with db_session() as db:
            db.add(CiFinding(repo="o/r", kind="flaky_test", fingerprint="a",
                             status="resolved", resolved_at=old))       # prune
            db.add(CiFinding(repo="o/r", kind="flaky_test", fingerprint="b",
                             status="resolved", resolved_at=recent))    # keep (recent)
            db.add(CiFinding(repo="o/r", kind="flaky_test", fingerprint="c",
                             status="open", resolved_at=None))          # keep (open)
        await loop._prune({"retention_days": 90})
        async with db_session() as db:
            fps = {r.fingerprint for r in (await db.execute(
                select(CiFinding))).scalars().all()}
        assert fps == {"b", "c"}
    asyncio.run(run())


def test_orphan_repo_findings_resolve_on_tick(tmp_path):
    """A finding for a repo the doctor no longer watches (removed from
    enabled_repos — or never real: 23 `o/r` fixture rows leaked into prod while
    the suite ran against live Postgres, 2026-08-25..31) is reconciled by no
    per-repo pass and pruned by nothing, so it sits open forever and counts on
    the card. The tick resolves it; the linked issue, if any, is left alone."""
    async def run():
        await init_db(f"sqlite+aiosqlite:///{tmp_path}/t.db")
        ctx = AutomationContext("cicd-doctor")
        gh = FakeGh()
        loop = _loop(ctx, runs=[], junit_by_run={}, gh=gh, repos=["o/r"])
        async with db_session() as db:
            db.add(CiFinding(repo="gone/repo", kind="flaky_test", fingerprint="a",
                             status="open", title="flaky test", issue_number=5))
            db.add(CiFinding(repo="o/r", kind="flaky_test", fingerprint="b",
                             status="open", title="flaky test"))
        # The sweep alone: the watched repo's row is not its business.
        await loop._resolve_orphans(["o/r"])
        async with db_session() as db:
            rows = {r.fingerprint: r for r in (await db.execute(
                select(CiFinding))).scalars().all()}
        assert rows["a"].status == "resolved" and rows["a"].resolved_at is not None
        assert rows["b"].status == "open"
        # And it runs as part of a tick (a tick with no runs also resolves `b`
        # through the ordinary per-repo reconcile — that path is covered above).
        async with db_session() as db:
            db.add(CiFinding(repo="gone/repo", kind="flaky_test", fingerprint="c",
                             status="open", title="flaky test"))
        await loop.tick()
        async with db_session() as db:
            c = (await db.execute(select(CiFinding).where(
                CiFinding.fingerprint == "c"))).scalar_one()
        assert c.status == "resolved"
        assert gh.closed == []                 # no GitHub side effect for an orphan
    asyncio.run(run())


def test_empty_repo_list_resolves_nothing(tmp_path):
    """An empty repo list (transient config/list failure) must not be read as
    'every repo is gone' and resolve the whole table."""
    async def run():
        await init_db(f"sqlite+aiosqlite:///{tmp_path}/t.db")
        ctx = AutomationContext("cicd-doctor")
        loop = _loop(ctx, runs=[], junit_by_run={}, repos=[])
        async with db_session() as db:
            db.add(CiFinding(repo="o/r", kind="flaky_test", fingerprint="a",
                             status="open", title="flaky test"))
        await loop.tick()
        async with db_session() as db:
            row = (await db.execute(select(CiFinding))).scalar_one()
        assert row.status == "open"
    asyncio.run(run())


def test_non_reentrant(tmp_path):
    async def run():
        await init_db(f"sqlite+aiosqlite:///{tmp_path}/t.db")
        ctx = AutomationContext("cicd-doctor")
        loop = _loop(ctx, runs=_runs(1), junit_by_run={})
        await asyncio.gather(loop.tick(), loop.tick())  # must not double-ingest
        async with db_session() as db:
            from server.db.models import CiRun
            assert len((await db.execute(select(CiRun))).scalars().all()) == 1
    asyncio.run(run())
