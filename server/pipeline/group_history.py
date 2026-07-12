"""Merge-group run/failure history store (SPEC §1 — MERGEETASIM substrate).

Records the outcome of a completed merge_group CI run into two tables:

- ``group_runs``: one row per fully-green run (every check concluded
  SUCCESS|SKIPPED|NEUTRAL). ``duration_secs = max(completedAt) - min(startedAt)``.
  Deduped on (repo, completed_at) where completed_at = max(completedAt) ISO.
- ``group_failures``: one row per failing check (FAILURE|TIMED_OUT|STARTUP_FAILURE).
  Deduped on (repo, group_sha, check_name).

Both dedupe via SQLite ``INSERT ... ON CONFLICT DO NOTHING`` against the models'
unique constraints. Missing timestamps → skip quietly (honest gaps).

Consumed by Task 3 (mergeEtaSim) via the three read functions:
``group_run_samples``, ``count_group_runs``, ``count_group_ejects``.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from server.db import db_session
from server.db.models import GroupFailure, GroupRun

# Conclusions (UPPER-normalized) that count a check as cleanly passed.
_CLEAN_CONCLUSIONS = frozenset({"SUCCESS", "SKIPPED", "NEUTRAL"})
# Conclusions that count a check as a merge-group eject / failure.
_FAILURE_CONCLUSIONS = frozenset({"FAILURE", "TIMED_OUT", "STARTUP_FAILURE"})


def _iso_seconds(iso: str) -> float:
    """Parse an ISO-8601 timestamp (trailing Z tolerated) to epoch seconds."""
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def _get(check: dict, snake: str, camel: str) -> object:
    """Read a check field tolerating snake_case (parser output) or camelCase."""
    val = check.get(snake)
    if val is None:
        val = check.get(camel)
    return val


async def record_group_outcome(
    repo: str, group_sha: str, checks: list[dict], now_iso: str
) -> None:
    """Record one merge-group run's outcome (SPEC §1).

    All checks terminal AND every conclusion clean → one GroupRun (deduped on
    (repo, completed_at)). Any failing conclusion → a GroupFailure per failing
    check (deduped on (repo, group_sha, check_name)). Missing timestamps or a
    non-terminal group → recorded nothing (honest gaps).
    """
    if not checks:
        return

    conclusions = [
        str(_get(c, "conclusion", "conclusion") or "").upper() for c in checks
    ]

    # A merge-group run is only recordable once every check has concluded.
    if any(not concl for concl in conclusions):
        return

    failing = [
        (c, concl)
        for c, concl in zip(checks, conclusions)
        if concl in _FAILURE_CONCLUSIONS
    ]

    if failing:
        await _record_failures(repo, group_sha, failing, now_iso)
        return

    # Fully-green only when EVERY check is clean (a stray unknown terminal
    # conclusion — e.g. ACTION_REQUIRED, CANCELLED — is neither clean nor a
    # tracked failure, so the run is not a clean sample).
    if not all(concl in _CLEAN_CONCLUSIONS for concl in conclusions):
        return

    await _record_clean_run(repo, checks)


async def _record_clean_run(repo: str, checks: list[dict]) -> None:
    starts: list[str] = []
    completes: list[str] = []
    for c in checks:
        started = _get(c, "started_at", "startedAt")
        completed = _get(c, "completed_at", "completedAt")
        if not started or not completed:
            # Missing timestamp → can't measure duration → honest gap.
            return
        starts.append(str(started))
        completes.append(str(completed))

    min_started = min(_iso_seconds(s) for s in starts)
    completed_secs = [(_iso_seconds(c), c) for c in completes]
    max_completed_secs, max_completed_iso = max(completed_secs, key=lambda t: t[0])
    duration = int(round(max_completed_secs - min_started))

    stmt = (
        sqlite_insert(GroupRun)
        .values(repo=repo, duration_secs=duration, completed_at=max_completed_iso)
        .on_conflict_do_nothing(index_elements=["repo", "completed_at"])
    )
    async with db_session() as db:
        await db.execute(stmt)


async def _record_failures(
    repo: str, group_sha: str, failing: list[tuple[dict, str]], now_iso: str
) -> None:
    async with db_session() as db:
        for c, concl in failing:
            name = str(_get(c, "name", "name") or "")
            if not name:
                continue
            stmt = (
                sqlite_insert(GroupFailure)
                .values(
                    repo=repo,
                    check_name=name,
                    group_sha=group_sha,
                    observed_at=now_iso,
                    conclusion=concl,
                )
                .on_conflict_do_nothing(
                    index_elements=["repo", "group_sha", "check_name"]
                )
            )
            await db.execute(stmt)


async def group_run_samples(repo: str, limit: int = 20) -> list[int]:
    """Last-``limit`` clean group-run durations for ``repo``, newest first."""
    stmt = (
        select(GroupRun.duration_secs)
        .where(GroupRun.repo == repo, GroupRun.duration_secs.isnot(None))
        .order_by(GroupRun.completed_at.desc())
        .limit(limit)
    )
    async with db_session() as db:
        rows = (await db.execute(stmt)).scalars().all()
    return [int(d) for d in rows if d is not None]


async def count_group_runs(repo: str, since_iso: str) -> int:
    """Count clean group runs for ``repo`` with completed_at >= since_iso."""
    stmt = select(func.count()).select_from(GroupRun).where(
        GroupRun.repo == repo, GroupRun.completed_at >= since_iso
    )
    async with db_session() as db:
        return int((await db.execute(stmt)).scalar_one())


async def count_group_ejects(repo: str, since_iso: str) -> int:
    """Count DISTINCT ejected group_sha for ``repo`` with observed_at >= since_iso."""
    stmt = select(func.count(func.distinct(GroupFailure.group_sha))).where(
        GroupFailure.repo == repo, GroupFailure.observed_at >= since_iso
    )
    async with db_session() as db:
        return int((await db.execute(stmt)).scalar_one())
