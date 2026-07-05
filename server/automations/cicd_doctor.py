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
                    default=21),
        ConfigField(key="min_runs", label="Graduation min runs", type="int", default=30),
        ConfigField(key="max_flake_pct", label="Graduation max flake %", type="int", default=2),
        ConfigField(key="max_p95_minutes", label="Graduation max p95 (min)", type="int",
                    default=10),
        ConfigField(key="demote_flake_pct", label="Demotion flake %", type="int", default=5,
                    help="Sustained over the demotion window (hysteresis vs graduation)."),
        ConfigField(key="demote_window_days", label="Demotion window (days)", type="int",
                    default=7),
        ConfigField(key="retire_days", label="Retirement age (days)", type="int", default=60),
        ConfigField(key="flaky_test_min_pct", label="Flaky-test threshold %", type="int",
                    default=2),
        ConfigField(key="retention_days", label="History retention (days)", type="int",
                    default=90),
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
                 clock: Callable[[], float]) -> None:
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
        from .cicd.junit import parse_junit_xml

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
                for suite, xml in await self._fetch_junit(repo, r["id"]):
                    try:
                        cases = parse_junit_xml(xml)
                    except ValueError:
                        log.warning("cicd_doctor.junit_malformed repo=%s run=%s",
                                    repo, r["id"])
                        continue
                    for c in cases:
                        db.add(CiTestResult(repo=repo, run_id=r["id"], suite=suite,
                                            test_id=c.test_id, outcome=c.outcome,
                                            time_s=c.time_s, head_sha=r["head_sha"],
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
        from .cicd.stats import check_stats, test_flake_pcts
        from .cicd.types import GateThresholds, JobObs, TestObs
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
        # Select column tuples, not whole ORM entities: the analyzers only read a
        # handful of columns and build detached value objects, so hydrating full
        # mapped instances (identity map, all columns) is pure overhead at 10^5+ rows.
        async with db_session() as db:
            job_rows = (await db.execute(
                select(CiJob.name, CiJob.head_sha, CiJob.head_branch, CiJob.conclusion,
                       CiJob.duration_s, CiJob.created_ts)
                .where(CiJob.repo == repo, CiJob.created_ts >= since_ts))).all()
            test_rows = (await db.execute(
                select(CiTestResult.test_id, CiTestResult.head_sha,
                       CiTestResult.head_branch, CiTestResult.outcome)
                .where(CiTestResult.repo == repo, CiTestResult.created_ts >= since_ts))).all()
            run_count = (await db.execute(
                select(func.count()).select_from(CiRun)
                .where(CiRun.repo == repo, CiRun.created_ts >= since_ts))).scalar_one()
        default_branch, required = await self._fetch_repo_meta(repo)
        job_obs = [JobObs(check=r.name or "", head_sha=r.head_sha or "",
                          head_branch=r.head_branch or "", conclusion=r.conclusion or "",
                          duration_s=r.duration_s or 0.0, created_ts=r.created_ts or 0.0)
                   for r in job_rows]
        test_obs = [TestObs(test_id=r.test_id or "", head_sha=r.head_sha or "",
                            head_branch=r.head_branch or "", outcome=r.outcome or "")
                    for r in test_rows]
        stats = check_stats(job_obs, default_branch=default_branch, now_ts=now,
                            demote_window_days=th.demote_window_days)
        verdicts = {c: v for c, s in stats.items()
                    if (v := check_verdict(s, is_required=c in required, th=th))}
        fresh = flaky_test_findings(repo, test_flake_pcts(test_obs), th=th)
        fresh += check_transition_findings(repo, verdicts)
        for path, text in (await self._fetch_workflows(repo, default_branch)).items():
            fresh += workflow_waste_findings(repo, path, text)
        if run_count and not test_rows:
            fresh.append(missing_junit_finding(repo))
        return fresh

    async def _reconcile(self, repo: str, fresh: list, config: dict) -> tuple[int, int]:
        import json as _json
        from sqlalchemy import select, update as sa_update
        from server.db import db_session
        from server.db.models import CiFinding
        from server.db.models import _utcnow  # single source for tz-aware now

        fresh_by_fp = {f.fingerprint: f for f in fresh}

        # Pass 1: compute all DB upserts/resolves; collect side effects for after commit.
        to_emit: list[tuple] = []          # (level, summary, kind, target, detail)
        to_file: list[tuple] = []          # (fingerprint, title, body)
        to_close: list[int] = []           # issue numbers to close
        open_n = candidates = 0

        async with db_session() as db:
            open_rows = (await db.execute(select(CiFinding).where(
                CiFinding.repo == repo, CiFinding.status == "open"))).scalars().all()
            open_by_fp = {r.fingerprint: r for r in open_rows}
            for fp, f in fresh_by_fp.items():
                row = open_by_fp.get(fp)
                if row is None:
                    row = CiFinding(repo=repo, kind=f.kind, fingerprint=fp,
                                    status="open", title=f.title, detail=f.detail)
                    db.add(row)
                    to_emit.append(("info", f"{repo}: {f.title}", "detected", fp, f.detail))
                    if config.get("file_issues") and f.actionable:
                        body = (f"{f.title}\n\n```json\n"
                                f"{_json.dumps(f.detail, indent=2)}\n```\n\n"
                                "Filed by the ccswitch CI/CD Doctor (propose mode).")
                        to_file.append((fp, f.title, body))
                else:
                    row.title, row.detail, row.last_seen_at = f.title, f.detail, _utcnow()
                    # An open row with no issue number means a prior filing attempt
                    # failed (e.g. transient gh error) — retry it this tick.
                    if (row.issue_number is None and config.get("file_issues")
                            and f.actionable):
                        body = (f"{f.title}\n\n```json\n"
                                f"{_json.dumps(f.detail, indent=2)}\n```\n\n"
                                "Filed by the ccswitch CI/CD Doctor (propose mode).")
                        to_file.append((fp, f.title, body))
            for fp, row in open_by_fp.items():
                if fp not in fresh_by_fp:
                    row.status, row.resolved_at = "resolved", _utcnow()
                    to_emit.append(("info", f"{repo}: resolved — {row.title}",
                                    "recovered", fp, None))
                    if row.issue_number:
                        to_close.append(row.issue_number)
            open_n = sum(1 for fp in fresh_by_fp)
            candidates = sum(1 for f in fresh_by_fp.values() if f.kind == "graduate_check")
        # Pass 1 session committed; SQLite write lock released.

        # Pass 2: run side effects now that the DB write lock is free.
        for level, summary, kind, target, detail in to_emit:
            await self._ctx.emit(level, summary, kind=kind, target=target, detail=detail)
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
        for issue_number in to_close:
            await self._close_issue(repo, issue_number)

        return open_n, candidates

    async def _prune(self, config: dict) -> None:
        """Bounded tables: drop history rows older than retention_days."""
        from sqlalchemy import delete
        from server.db import db_session
        from server.db.models import CiJob, CiRun, CiTestResult

        horizon = self._clock() - float(config.get("retention_days", 90)) * 86400.0
        async with db_session() as db:
            for model in (CiRun, CiJob, CiTestResult):
                await db.execute(delete(model).where(model.created_ts < horizon))
