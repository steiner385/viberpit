"""The cicd-doctor automation: system-level CI health — flaky tests, advisory-
check graduation, waste. Proactive/per-system, where pr-doctor is reactive/
per-PR (it never touches an individual PR). v1 is propose-only: every verdict
is a finding row + optional ci-doctor GitHub issue, never a mutation."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from .base import AutomationStatus, ConfigField

log = logging.getLogger(__name__)

# Rows folded per tick. Bounds the transaction so a tick never rewrites millions of
# rows at once; the backlog is drained by scripts/ccswitch-db-compact.py. The
# delete uses id.in_([...]), one bound parameter per row — kept well under
# SQLite's 32766-parameter ceiling (20_000 was only 1.6x away from it).
_ROLLUP_BATCH = 10_000


class CicdDoctor:
    id = "cicd-doctor"
    name = "CI/CD Doctor"
    description = ("Watches CI health across repos: flaky tests, advisory-check "
                   "stability and graduation, and workflow waste.")
    modes: tuple[str, ...] = ("off", "propose")
    config_schema = [
        ConfigField(key="enabled_repos", label="Repositories", type="repo-multiselect",
                    default=[], help="Limit to these repos (empty = all ccswitch-managed)."),
        ConfigField(key="file_issues", label="File GitHub issues", type="bool", default=True,
                    help="Actionable findings become ci-doctor labeled issues."),
        ConfigField(key="min_window_days", label="Graduation window (days)", type="int",
                    default=21,
                    help="Minimum observation period (days) before an advisory check can graduate to required."),
        ConfigField(key="min_runs", label="Graduation min runs", type="int", default=30,
                    help="Minimum CI runs in the window required before graduation is considered."),
        ConfigField(key="max_flake_pct", label="Graduation max flake %", type="int", default=2,
                    help="Maximum flake rate (%) a check may have and still qualify for graduation."),
        ConfigField(key="max_p95_minutes", label="Graduation max p95 (min)", type="int",
                    default=10,
                    help="Maximum p95 runtime (minutes) a check may have and still qualify for graduation."),
        ConfigField(key="demote_flake_pct", label="Demotion flake %", type="int", default=5,
                    help="Flake rate (%) that triggers demotion — must be sustained over the demotion window (hysteresis vs graduation)."),
        ConfigField(key="demote_window_days", label="Demotion window (days)", type="int",
                    default=7,
                    help="Look-back window (days) over which demotion flake rate is measured."),
        ConfigField(key="retire_days", label="Retirement age (days)", type="int", default=60,
                    help="Advisory checks not seen in any CI run for this many days are flagged for retirement."),
        ConfigField(key="flaky_test_min_pct", label="Flaky-test threshold %", type="int",
                    default=2,
                    help="Tests flaking above this rate (%) are surfaced as actionable findings."),
        ConfigField(key="retention_days", label="History retention (days)", type="int",
                    default=90,
                    help="CI run history older than this is pruned from the local database."),
        ConfigField(key="raw_retention_days", label="Raw test-result window (days)",
                    type="int", default=7,
                    help="Keep individual passed/skipped test rows this long, then fold "
                         "them into per-day counts. Failed rows are kept raw for the "
                         "full retention window."),
    ]

    def status(self, ctx: Any) -> AutomationStatus:
        counts = getattr(ctx, "cicd_doctor_counts", None) if ctx is not None else None
        if not counts:
            return AutomationStatus(text="idle", level="idle")
        open_n, cand = counts.get("open", 0), counts.get("candidates", 0)
        errors = counts.get("errors", 0)
        bits = [f"{open_n} open finding(s)"]
        if cand:
            bits.append(f"{cand} graduation candidate(s)")
        if errors:
            bits.append(f"{errors} repo error(s)")
        if open_n:
            level = "active"
        elif errors:
            level = "warn"
        else:
            level = "idle"
        return AutomationStatus(text=", ".join(bits), level=level, counts=counts)

    async def on_state_change(self, *, enabled: bool, mode: str | None,
                              config: dict, ctx: Any) -> None:
        # The orchestrator's loop reads mode/config each tick; nothing to start here.
        ...


def flake_counts_stmt(repo: str, since_ts: float):
    """SELECT (test_id, distinct_shas, shas_with_both_a_pass_and_a_fail) for *repo*.

    Collapses the whole window to ONE ROW PER TEST inside the database. The
    previous code streamed every raw attempt row into Python and folded it with
    ``test_flake_pcts``; on 2026-08-29 that was 14,530,499 rows for a single repo
    over a 7-day window (26.4M-row table). Because SQLAlchemy's async layer runs
    ORM row construction via greenlet on the CALLING thread, that fold happened on
    the daemon's event loop — 624 loop blocks in 12h, worst 99.02s, during which
    the daemon served nothing and every session socket hung.

    ``SELECT DISTINCT`` is not a substitute: 33,798 distinct tests x 320 distinct
    shas leaves ~10.8M distinct triples, barely a quarter off. The aggregate has
    to happen in SQL.

    Two levels, because flakiness is a property of a (test, sha) PAIR — a sha that
    shows both a pass and a fail — not of a single row:
      inner: one row per (test_id, head_sha), flagging whether it saw a pass / a fail
      outer: one row per test_id, counting shas and the both-flagged subset

    ``max(CASE ...)`` rather than ``bool_or``/``FILTER`` so the same statement runs
    on SQLite and Postgres alike (this DB is reconciled on both — see CLAUDE.md).
    Outcome filtering reuses the exported sets from ``stats`` so the SQL and the
    in-memory fold cannot drift.
    """
    from sqlalchemy import case, func, select

    from server.db.models import CiTestResult
    from .cicd.stats import FAIL_OUTCOMES, FLAKE_OUTCOMES, PASS_OUTCOMES

    saw_pass = func.max(case((CiTestResult.outcome.in_(sorted(PASS_OUTCOMES)), 1),
                             else_=0)).label("saw_pass")
    saw_fail = func.max(case((CiTestResult.outcome.in_(sorted(FAIL_OUTCOMES)), 1),
                             else_=0)).label("saw_fail")
    per_sha = (
        select(CiTestResult.test_id.label("test_id"), saw_pass, saw_fail)
        .where(CiTestResult.repo == repo,
               CiTestResult.created_ts >= since_ts,
               CiTestResult.outcome.in_(sorted(FLAKE_OUTCOMES)))
        .group_by(CiTestResult.test_id, CiTestResult.head_sha)
        .subquery()
    )
    return (
        select(
            per_sha.c.test_id,
            func.count().label("shas"),
            func.sum(case(((per_sha.c.saw_pass == 1) & (per_sha.c.saw_fail == 1), 1),
                          else_=0)).label("flaky_shas"),
        )
        .group_by(per_sha.c.test_id)
    )


def latest_failure_messages_stmt(repo: str, since_ts: float):
    """SELECT (test_id, message) — the most recent failing message per test.

    One row per test, not per attempt. The message is only ever shown attached to
    a flaky-test finding, so fetching every failing row to keep "whichever the scan
    saw last" was both unbounded and arbitrary; ``row_number()`` makes it bounded
    and deterministic (newest wins). Window functions are available on both
    SQLite (3.25+) and Postgres.

    The outcome filter stays ``failed``/``error`` — deliberately NARROWER than
    ``FAIL_OUTCOMES`` — to preserve the original behaviour exactly.
    """
    from sqlalchemy import func, select

    from server.db.models import CiTestResult

    rn = func.row_number().over(
        partition_by=CiTestResult.test_id,
        order_by=CiTestResult.created_ts.desc(),
    ).label("rn")
    ranked = (
        select(CiTestResult.test_id.label("test_id"),
               CiTestResult.message.label("message"), rn)
        .where(CiTestResult.repo == repo,
               CiTestResult.created_ts >= since_ts,
               CiTestResult.outcome.in_(("failed", "error")),
               CiTestResult.message.is_not(None))
        .subquery()
    )
    return select(ranked.c.test_id, ranked.c.message).where(ranked.c.rn == 1)


class CicdDoctorLoop:
    """Restart-safe, non-reentrant sense+advise loop. Every I/O dependency is
    injected. Per tick and per repo: ingest new completed runs (cursor in
    automation_target_state), run the pure analyzers over stored history, then
    reconcile findings (open/update/resolve + file/close ci-doctor issues)."""

    def __init__(self, ctx, *,
                 read_config: Callable[[], Awaitable[tuple[str, dict]]],
                 list_repos: Callable[[dict], Awaitable[list[str]]],
                 fetch_runs: Callable[[str, float | None], Awaitable[list[dict]]],
                 fetch_jobs: Callable[[str, int], Awaitable[list[dict]]],
                 fetch_junit: Callable[[str, int], Awaitable[list[tuple[str, bytes]]]],
                 fetch_repo_meta: Callable[[str], Awaitable[tuple[str, set[str]]]],
                 fetch_workflows: Callable[[str, str], Awaitable[dict[str, str]]],
                 file_issue: Callable[[str, str, str], Awaitable[int | None]],
                 close_issue: Callable[[str, int], Awaitable[bool]],
                 clock: Callable[[], float],
                 reopen_issue: Callable[[str, int], Awaitable[bool]] | None = None) -> None:
        self._ctx = ctx
        self._read_config = read_config
        self._list_repos = list_repos
        self._fetch_runs = fetch_runs
        self._fetch_jobs = fetch_jobs
        self._fetch_junit = fetch_junit
        self._fetch_repo_meta = fetch_repo_meta
        self._fetch_workflows = fetch_workflows
        self._file_issue = file_issue
        self._close_issue = close_issue
        self._reopen_issue = reopen_issue
        self._clock = clock
        self._lock = asyncio.Lock()

    async def tick(self) -> None:
        async with self._lock:
            try:
                await self._run()
            except Exception:  # a tick must never crash the loop
                log.warning("cicd_doctor.tick_failed", exc_info=True)

    async def _run(self) -> None:
        mode, config = await self._read_config()
        if mode == "off":
            return
        open_total = candidates = errors = 0
        for repo in await self._list_repos(config):
            try:
                await self._ingest(repo)
                fresh = await self._analyze(repo, config)
                o, c = await self._reconcile(repo, fresh, config)
                open_total += o
                candidates += c
            except Exception:  # one repo must never stall the tick
                errors += 1
                log.warning("cicd_doctor.repo_failed repo=%s", repo, exc_info=True)
        await self._prune(config)
        setattr(self._ctx, "cicd_doctor_counts",
                {"open": open_total, "candidates": candidates, "errors": errors})

    async def _ingest(self, repo: str) -> None:
        from sqlalchemy import select
        from server.db import db_session
        from server.db.models import CiJob, CiRun, CiTestResult
        from .cicd.junit import JUnitCase, parse_junit_xml

        cursor = await self._ctx.get_target_state(f"cursor:{repo}")
        since = cursor.get("last_created_ts")
        runs = await self._fetch_runs(repo, since)
        if not runs:
            return
        max_ts = since or 0.0
        async with db_session() as db:
            seen = set((await db.execute(select(CiRun.id).where(
                CiRun.repo == repo))).scalars().all())
            for r in runs:
                max_ts = max(max_ts, r["created_ts"])
                if r["id"] in seen:
                    continue
                db.add(CiRun(id=r["id"], repo=repo, workflow_path=r["workflow_path"],
                             event=r["event"], head_sha=r["head_sha"],
                             head_branch=r["head_branch"], conclusion=r["conclusion"],
                             created_ts=r["created_ts"], duration_s=r["duration_s"]))
                for j in await self._fetch_jobs(repo, r["id"]):
                    db.add(CiJob(id=j["id"], run_id=r["id"], repo=repo, name=j["name"],
                                 conclusion=j["conclusion"], duration_s=j["duration_s"],
                                 head_sha=r["head_sha"], head_branch=r["head_branch"],
                                 created_ts=r["created_ts"]))
                # One <testcase> per ATTEMPT: a framework retry repeats the same
                # (suite, test_id). Keep the LAST attempt — it is the outcome the
                # run finished with — so ingestion is idempotent against
                # uq_ci_test_result_run_suite_test. 430,779 rows (3.6% of the
                # table) were duplicates before this.
                #
                # Keyed on (suite, test_id) and hoisted ABOVE the suite loop: if two
                # artifacts in one run yield the SAME suite string, keying on
                # test_id alone would let the second artifact's cases overwrite the
                # dict entry-by-entry but still emit one row per artifact pass,
                # producing two rows for the same (run_id, suite, test_id) and
                # violating uq_ci_test_result_run_suite_test on flush. Accumulating
                # across all suites first and emitting once keeps "last attempt
                # wins per suite+test" while staying idempotent against the
                # constraint even when a suite string repeats within one run. The
                # suite is already the key's first element, so the value only needs
                # to hold the case; suite is recovered from the key when emitting.
                last_attempt: dict[tuple[str, str], JUnitCase] = {}
                for suite, xml in await self._fetch_junit(repo, r["id"]):
                    try:
                        cases = parse_junit_xml(xml)
                    except ValueError:
                        log.warning("cicd_doctor.junit_malformed repo=%s run=%s",
                                    repo, r["id"])
                        continue
                    for c in cases:
                        last_attempt[(suite, c.test_id)] = c
                for (result_suite, _test_id), c in last_attempt.items():
                    db.add(CiTestResult(repo=repo, run_id=r["id"], suite=result_suite,
                                        test_id=c.test_id, outcome=c.outcome,
                                        time_s=c.time_s, message=c.message,
                                        head_sha=r["head_sha"],
                                        head_branch=r["head_branch"],
                                        created_ts=r["created_ts"]))
            await db.flush()
        await self._ctx.set_target_state(f"cursor:{repo}", {"last_created_ts": max_ts})

    async def _analyze(self, repo: str, config: dict) -> list:
        from sqlalchemy import func, select
        from server.db import db_session
        from server.db.models import CiJob, CiRun, CiTestResult
        from .cicd.findings import (check_transition_findings, flaky_test_findings,
                                    missing_junit_finding)
        from .cicd.gates import check_verdict
        from .cicd.types import GateThresholds, JobObs
        from .cicd.waste import workflow_waste_findings

        th = GateThresholds.from_config(config)
        now = self._clock()
        # Cap the query to the widest window any analyzer looks at, so a tick
        # never materializes the full retained history (#449). retention_days is
        # the prune horizon and normally the widest; retire/graduation windows are
        # included so a small retention can't silently starve those verdicts.
        horizon_days = max(float(config.get("retention_days", 90)),
                           th.retire_days, th.min_window_days, th.demote_window_days)
        since_ts = now - horizon_days * 86400.0
        # Flakiness is the % of head_shas showing BOTH a pass and a fail over the
        # test_rows window. _rollup_passed folds/deletes raw passed rows older than
        # raw_retention_days, so no sha older than that can ever show a pass again
        # — while old fail-only shas remain. Querying test_rows over the full
        # horizon_days window would therefore silently under-report flakiness (the
        # denominator keeps old fail-only shas with no matching pass evidence).
        # CiTestDaily can't fix this: it drops head_sha, the dimension flakiness is
        # computed over. Clamp this query ALONE to the window where pass evidence
        # still exists; job/run queries below keep the wider horizon_days window.
        raw_retention_days = float(config.get("raw_retention_days", 7))
        test_since_ts = now - raw_retention_days * 86400.0
        # Select column tuples, not whole ORM entities: the analyzers only read a
        # handful of columns and build detached value objects, so hydrating full
        # mapped instances (identity map, all columns) is pure overhead at 10^5+ rows.
        async with db_session() as db:
            job_rows = (await db.execute(
                select(CiJob.name, CiJob.head_sha, CiJob.head_branch, CiJob.conclusion,
                       CiJob.duration_s, CiJob.created_ts)
                .where(CiJob.repo == repo, CiJob.created_ts >= since_ts))).all()
            # Aggregated in SQL to one row per test — see flake_counts_stmt for why
            # the raw-row fold had to go.
            flake_rows = (await db.execute(
                flake_counts_stmt(repo, test_since_ts))).all()
            msg_rows = (await db.execute(
                latest_failure_messages_stmt(repo, test_since_ts))).all()
            # missing_junit_finding asks whether ANY raw test row exists in the
            # window — including outcomes the flake aggregate filters out — so it
            # needs its own probe. LIMIT 1: existence, not a count.
            any_test_row = (await db.execute(
                select(CiTestResult.id)
                .where(CiTestResult.repo == repo,
                       CiTestResult.created_ts >= test_since_ts)
                .limit(1))).first() is not None
            run_count = (await db.execute(
                select(func.count()).select_from(CiRun)
                .where(CiRun.repo == repo, CiRun.created_ts >= since_ts))).scalar_one()
        default_branch, required = await self._fetch_repo_meta(repo)

        # The pure-Python folds below run in ONE worker thread, not on the loop.
        # #791 moved the CiTestResult aggregation into SQL, but job_rows stayed
        # materialized: check_stats over a 90-day CiJob window (292k-row table)
        # is tens of seconds of uninterruptible CPU, and the stall tracer caught
        # the loop wedged exactly here (issue #827 — 37.7s/23.1s/21.5s/20.5s in
        # one day, each freezing every edge request and session socket). All
        # inputs are detached row tuples and the fold is side-effect-free, so a
        # thread is safe by construction. Re-binding through the module (not the
        # function refs imported above) keeps monkeypatched seams honest — the
        # off-loop regression test spies via the module attribute.
        def _fold():
            from .cicd import stats as _stats
            job_obs = [JobObs(check=r.name or "", head_sha=r.head_sha or "",
                              head_branch=r.head_branch or "",
                              conclusion=r.conclusion or "",
                              duration_s=r.duration_s or 0.0,
                              created_ts=r.created_ts or 0.0)
                       for r in job_rows]
            # A representative failure message per test (most recent wins) —
            # attached to the flaky-test finding so it points at a concrete
            # failure, not just a %.
            msg_by_test = {r.test_id or "": r.message for r in msg_rows}
            flake_pcts = _stats.flake_pcts_from_sha_counts(
                [(r[0] or "", int(r[1] or 0), int(r[2] or 0)) for r in flake_rows])
            stats = _stats.check_stats(job_obs, default_branch=default_branch,
                                       now_ts=now,
                                       demote_window_days=th.demote_window_days)
            verdicts = {c: v for c, s in stats.items()
                        if (v := check_verdict(s, is_required=c in required, th=th))}
            out = flaky_test_findings(repo, flake_pcts, th=th, messages=msg_by_test)
            out += check_transition_findings(repo, verdicts)
            return out

        fresh = await asyncio.to_thread(_fold)
        for path, text in (await self._fetch_workflows(repo, default_branch)).items():
            fresh += workflow_waste_findings(repo, path, text)
        if run_count and not any_test_row:
            fresh.append(missing_junit_finding(repo))
        return fresh

    async def _reconcile(self, repo: str, fresh: list, config: dict) -> tuple[int, int]:
        from sqlalchemy import false, or_, select, update as sa_update
        from server.db import db_session
        from server.db.models import CiFinding
        from server.db.models import _utcnow  # single source for tz-aware now
        from .cicd.findings import issue_body

        fresh_by_fp = {f.fingerprint: f for f in fresh}
        file_on = bool(config.get("file_issues"))

        def _body(f) -> str:
            return issue_body(f.title, f.detail)

        # Pass 1: compute all DB upserts/resolves; collect side effects for after commit.
        to_emit: list[tuple] = []          # (level, summary, kind, target, detail)
        to_file: list[tuple] = []          # (fingerprint, title, body) -> file new issue
        to_reopen: list[int] = []          # issue numbers to reopen on recurrence
        open_n = candidates = 0

        async with db_session() as db:
            open_rows = (await db.execute(select(CiFinding).where(
                CiFinding.repo == repo, CiFinding.status == "open"))).scalars().all()
            open_by_fp = {r.fingerprint: r for r in open_rows}
            # Latest resolved row per fingerprint that still links a filed issue —
            # the reopen target when a finding flaps back (#448).
            resolved_rows = (await db.execute(select(CiFinding).where(
                CiFinding.repo == repo, CiFinding.status == "resolved",
                CiFinding.issue_number.is_not(None))
                .order_by(CiFinding.resolved_at.desc()))).scalars().all()
            resolved_by_fp: dict[str, CiFinding] = {}
            for r in resolved_rows:
                if r.fingerprint is not None:
                    resolved_by_fp.setdefault(r.fingerprint, r)  # first == most recent

            for fp, f in fresh_by_fp.items():
                row = open_by_fp.get(fp)
                if row is not None:
                    row.title, row.detail, row.last_seen_at = f.title, f.detail, _utcnow()
                    # An open row with no issue number means a prior filing attempt
                    # failed (e.g. transient gh error) — retry it this tick.
                    if row.issue_number is None and file_on and f.actionable:
                        to_file.append((fp, f.title, _body(f)))
                    continue
                prior = resolved_by_fp.get(fp)
                if prior is not None and file_on and f.actionable:
                    # Recurrence of a previously-filed finding: revive the resolved
                    # row and reopen its issue instead of filing a duplicate (#448).
                    prior.status, prior.resolved_at, prior.issue_closed = "open", None, None
                    prior.title, prior.detail, prior.last_seen_at = f.title, f.detail, _utcnow()
                    to_reopen.append(prior.issue_number)  # type: ignore[arg-type]
                    to_emit.append(("info", f"{repo}: recurred — {f.title}",
                                    "detected", fp, f.detail))
                else:
                    db.add(CiFinding(repo=repo, kind=f.kind, fingerprint=fp,
                                     status="open", title=f.title, detail=f.detail))
                    to_emit.append(("info", f"{repo}: {f.title}", "detected", fp, f.detail))
                    if file_on and f.actionable:
                        to_file.append((fp, f.title, _body(f)))
            for fp, row in open_by_fp.items():
                if fp not in fresh_by_fp:
                    # Mark resolved; the close-pending sweep below closes the issue
                    # (and retries on failure) — issue_closed stays NULL until confirmed.
                    row.status, row.resolved_at = "resolved", _utcnow()
                    to_emit.append(("info", f"{repo}: resolved — {row.title}",
                                    "recovered", fp, None))
            open_n = len(fresh_by_fp)
            candidates = sum(1 for f in fresh_by_fp.values() if f.kind == "graduate_check")
        # Pass 1 session committed; SQLite write lock released.

        # Pass 2: run side effects now that the DB write lock is free.
        for level, summary, kind, target, detail in to_emit:
            await self._ctx.emit(level, summary, kind=kind, target=target, detail=detail)
        for reopen_number in to_reopen:
            if self._reopen_issue is not None:
                await self._reopen_issue(repo, reopen_number)
        for fp, title, body in to_file:
            issue_number = await self._file_issue(repo, title, body)
            if issue_number is not None:
                # Write issue_number back in a short second session (match on fingerprint).
                async with db_session() as db:
                    await db.execute(
                        sa_update(CiFinding)
                        .where(CiFinding.repo == repo, CiFinding.fingerprint == fp,
                               CiFinding.status == "open")
                        .values(issue_number=issue_number))
        # Close-pending sweep: every resolved finding whose linked issue is still
        # (believed) open. A first close and a retry of a previously-failed close
        # are the same operation here — a failed close leaves issue_closed NULL and
        # is retried next tick, never left open forever (#448).
        async with db_session() as db:
            pending = (await db.execute(select(CiFinding.id, CiFinding.issue_number).where(
                CiFinding.repo == repo, CiFinding.status == "resolved",
                CiFinding.issue_number.is_not(None),
                or_(CiFinding.issue_closed.is_(None),
                    CiFinding.issue_closed == false())))).all()
        for row_id, number in pending:
            if await self._close_issue(repo, number):
                async with db_session() as db:
                    await db.execute(sa_update(CiFinding)
                                     .where(CiFinding.id == row_id)
                                     .values(issue_closed=True))

        return open_n, candidates

    async def _rollup_passed(self, config: dict) -> None:
        """Fold raw passed/skipped rows older than raw_retention_days into
        CiTestDaily day-counts, then delete them — DRAINING the backlog, not
        folding a single batch. Failed AND error rows are never folded — findings
        quote their message, so they stay raw until retention_days. ("error" is a
        distinct outcome from "failed" — see junit.py's _OUTCOME_TAGS — but it is
        still a failure and must be excluded the same way.)

        Why a drain loop (#833): one batch per tick has a hard ceiling of
        _ROLLUP_BATCH rows per 900s ≈ 960k rows/day, and KinDash alone ingests
        ~2M/day. The fold silently lost that race for ~5 weeks: 12.3M stale rows
        (12.0M of them passed) in a 27.4M-row table designed to hold 7 days.
        Every consumer paid — the analyze aggregate read 40% of the table per
        tick, no index could help at that selectivity, and the DB hit 24 GB.

        So: keep folding batches until the backlog is empty or *rollup_budget_s*
        (config, default 20s) is spent. One session per batch, so progress
        commits incrementally and a crash or budget stop never double-counts.
        Stopping early logs a WARNING with the shortfall — a losing race must be
        visible long before the table is 4x oversized.

        Day bucketing happens in PYTHON, not SQL: this must behave identically on
        SQLite today and Postgres after sub-project 1b. The per-bucket lookup is
        one prefetch query per batch (not one per group, which at 10k groups was
        most of a batch's cost).
        """
        from collections import Counter
        from datetime import datetime, timezone

        from sqlalchemy import delete, select
        from server.db import db_session
        from server.db.models import CiTestDaily, CiTestResult

        raw_days = float(config.get("raw_retention_days", 7))
        budget_s = float(config.get("rollup_budget_s", 20.0))
        started = self._clock()
        horizon = started - raw_days * 86400.0
        batches = 0
        while True:
            async with db_session() as db:
                rows = (await db.execute(
                    select(CiTestResult.id, CiTestResult.repo, CiTestResult.test_id,
                           CiTestResult.outcome, CiTestResult.created_ts)
                    .where(CiTestResult.outcome.notin_(("failed", "error")),
                           CiTestResult.created_ts < horizon)
                    .limit(_ROLLUP_BATCH))).all()
                if not rows:
                    return
                counts: Counter = Counter()
                for _id, repo, test_id, outcome, ts in rows:
                    day = datetime.fromtimestamp(ts or 0.0, timezone.utc).strftime("%Y-%m-%d")
                    counts[(repo, test_id, day, outcome)] += 1
                # Prefetch every bucket this batch can touch in ONE query, then
                # match exact (repo, test_id, day) triples in Python. The IN-lists
                # over-fetch (cross product) but only among this batch's values.
                repos = {k[0] for k in counts}
                tests = {k[1] for k in counts}
                days = {k[2] for k in counts}
                existing = (await db.execute(select(CiTestDaily).where(
                    CiTestDaily.repo.in_(repos),
                    CiTestDaily.test_id.in_(tests),
                    CiTestDaily.day.in_(days)))).scalars().all()
                buckets = {(b.repo, b.test_id, b.day): b for b in existing}
                for (repo, test_id, day, outcome), n in counts.items():
                    bucket = buckets.get((repo, test_id, day))
                    if bucket is None:
                        bucket = CiTestDaily(repo=repo, test_id=test_id, day=day,
                                             passed=0, failed=0, skipped=0)
                        db.add(bucket)
                        await db.flush()
                        buckets[(repo, test_id, day)] = bucket
                    if outcome == "skipped":
                        bucket.skipped = (bucket.skipped or 0) + n
                    else:
                        bucket.passed = (bucket.passed or 0) + n
                await db.execute(delete(CiTestResult).where(
                    CiTestResult.id.in_([r[0] for r in rows])))
            batches += 1
            full_batch = len(rows) >= _ROLLUP_BATCH
            if not full_batch:
                return  # short batch == backlog drained
            if self._clock() - started >= budget_s:
                log.warning(
                    "cicd rollup behind: budget %.1fs spent after %d batch(es) "
                    "(%d rows folded) with stale rows still pending — will "
                    "continue next tick", budget_s, batches,
                    batches * _ROLLUP_BATCH)
                return

    async def _prune(self, config: dict) -> None:
        """Bounded tables: drop history rows older than retention_days, and prune
        long-resolved findings so ci_finding doesn't grow without bound (#448)."""
        from datetime import timedelta

        from sqlalchemy import delete
        from server.db import db_session
        from server.db.models import CiFinding, CiJob, CiRun, CiTestResult
        from server.db.models import _utcnow

        # Fold passed/skipped into day-counts BEFORE the age-based deletes, so a row
        # is always counted before it can be dropped.
        await self._rollup_passed(config)

        retention_days = float(config.get("retention_days", 90))
        horizon = self._clock() - retention_days * 86400.0
        finding_cutoff = _utcnow() - timedelta(days=retention_days)
        async with db_session() as db:
            for model in (CiRun, CiJob, CiTestResult):
                await db.execute(delete(model).where(model.created_ts < horizon))
            # Only resolved findings are pruned; an open finding is never dropped
            # however old. resolved_at NULL (legacy rows) is left for a later tick.
            await db.execute(delete(CiFinding).where(
                CiFinding.status == "resolved",
                CiFinding.resolved_at.is_not(None),
                CiFinding.resolved_at < finding_cutoff))
