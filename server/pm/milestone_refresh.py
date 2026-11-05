"""Release-milestone refresh (spec 2026-11-05 §7–§8): run a ladder's GitHub readings
and write timestamped ``milestone_measurements`` rows.

One refresh = one batched GraphQL query (plus epic continuation pages) and one run
list per distinct ``(repo, workflow, branch)``. Each condition's reading is evaluated
at write time (``milestone_status.evaluate``); read-time freshness is the view's job.

Error rule (Review Focus 3): a reading that failed keeps the row's previous ``value``
and ``measured_at`` — its true age — and sets ``error``; ``fetched_at`` is always the
refresh time. ``manual`` rows are never written here. Product-agnostic.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from server.db import db_session
from server.db.models import MilestoneLadder, MilestoneMeasurement, ProjectRepo
from server.github.host_failures import log_gh_failure
from server.pm.milestone_def import LadderDef, Measure, from_json
from server.pm.milestone_github import (
    Target,
    build_ladder_query,
    epic_reading_key,
    ladder_conditions,
    leaves_done,
    parse_ladder_payload,
    ref_reading_key,
    streak_from_runs,
    sub_issues_epic,
)
from server.pm.milestone_status import Reading, evaluate
from server.time_iso import aware_utc

log = logging.getLogger(__name__)

#: An on-demand refresh is refused within this many seconds of the ladder's last fetch.
REFRESH_MIN_INTERVAL_S = 60
#: The loop refreshes a ladder whose newest fetch is this old (spec §8: every 10 min).
STALE_AFTER_S = 600
#: Sub-issue pages read per epic per refresh (100 each).
MAX_EPIC_PAGES = 10
#: Runs read per workflow; events are filtered here, so read the API's full page.
RUNS_LIMIT = 100
CI_METRIC_PHASE1_ERROR = "ci_metric is measured from Phase 2"
DEFAULT_STREAK_EVENTS: tuple[str, ...] = ("schedule", "workflow_dispatch")
#: The ``key`` of a milestone's leaf-list row (scope ``leaves:<milestone key>``).
LEAVES_KEY = "leaves"

GraphQL = Callable[[str, str], Awaitable[dict]]
ListRuns = Callable[..., list[dict]]
_Key = tuple[str, str]


@dataclass(frozen=True)
class RefreshResult:
    written: int
    errors: int


def leaves_scope(milestone_key: str) -> str:
    return f"leaves:{milestone_key}"


def _naive_utc(dt: datetime) -> datetime:
    aware = aware_utc(dt)
    assert aware is not None
    return aware.replace(tzinfo=None)


def _error(msg: str) -> Reading:
    return Reading(None, None, msg, None)


# ---------------------------------------------------------------- GitHub reads


def _all_errors(targets: Mapping[str, Target], msg: str) -> dict[_Key, Reading]:
    out: dict[_Key, Reading] = {}
    for t in targets.values():
        out[(t.scope, t.key)] = _error(msg)
        for key in t.reads:
            out[key] = _error(msg)
    return out


def _whole_failure(payload: Any) -> str | None:
    """A response with no ``data`` at all failed as a whole (e.g. a query error)."""
    if not isinstance(payload, Mapping):
        return "GraphQL returned no payload"
    if payload.get("data") is None:
        errors = payload.get("errors") or ()
        first = next((e.get("message") for e in errors if isinstance(e, Mapping) and e.get("message")), None)
        return str(first or "GraphQL returned no data")
    return None


async def _read_graphql(
    project_id: str, ladder: LadderDef, *, primary_repo: str, graphql: GraphQL, now: datetime
) -> tuple[dict[_Key, Reading], dict[str, list[dict]]]:
    """Readings by ``(scope, key)`` and leaf items by epic / milestone-ref key, with
    every epic's pages merged (at most ``MAX_EPIC_PAGES``) and its ``done`` recomputed."""
    doc, targets = build_ladder_query(ladder, primary_repo=primary_repo, now=now)
    if not targets:
        return {}, {}
    owner = primary_repo.partition("/")[0]
    try:
        payload = await graphql(owner, doc)
        failure = _whole_failure(payload)
        if failure is not None:
            raise RuntimeError(failure)
    except Exception as exc:
        log_gh_failure(log, str(exc), "milestone_refresh.graphql_failed project=%s", project_id,
                       demote_edge_failures=True)
        return _all_errors(targets, f"GitHub read failed: {exc}"), {}
    readings, leaves, cursors = parse_ladder_payload(payload, targets, now=now)

    epics = {a: t for a, t in targets.items() if t.kind == "epic"}
    pages = 1
    while cursors and pages < MAX_EPIC_PAGES:
        pages += 1
        doc, page_targets = build_ladder_query(ladder, primary_repo=primary_repo, now=now, cursors=cursors)
        try:
            payload = await graphql(owner, doc)
            failure = _whole_failure(payload)
            if failure is not None:
                raise RuntimeError(failure)
        except Exception as exc:
            for alias in cursors:
                t = epics[alias]
                readings[(t.scope, t.key)] = _error(f"sub-issues page {pages}: {exc}")
            break
        page_readings, page_leaves, next_cursors = parse_ladder_payload(payload, page_targets, now=now)
        for alias, t in page_targets.items():
            assert t.ref is not None
            page = page_readings[(t.scope, t.key)]
            if page.error is not None:
                readings[(t.scope, t.key)] = _error(f"sub-issues page {pages}: {page.error}")
                next_cursors.pop(alias, None)
                continue
            leaves.setdefault(t.ref.key, []).extend(page_leaves.get(t.ref.key, []))
        cursors = next_cursors
    if cursors:
        log.info("milestone_refresh.epic_pages_capped project=%s epics=%d", project_id, len(cursors))

    for t in epics.values():
        assert t.ref is not None
        r = readings.get((t.scope, t.key))
        if r is not None and r.error is None and r.value is not None:
            readings[(t.scope, t.key)] = Reading(
                {"done": leaves_done(leaves.get(t.ref.key, [])), "total": r.value["total"]},
                r.measured_at, None, r.source_url)
    return readings, leaves


def _streak_conditions(ladder: LadderDef, primary_repo: str) -> dict[tuple[str, str, str | None], list[tuple[_Key, tuple[str, ...]]]]:
    out: dict[tuple[str, str, str | None], list[tuple[_Key, tuple[str, ...]]]] = {}
    for scope, _ms, cond in ladder_conditions(ladder):
        if cond.measure.kind != "workflow_streak":
            continue
        p = cond.measure.params
        events = tuple(p.get("events") or DEFAULT_STREAK_EVENTS)
        out.setdefault((primary_repo, str(p["workflow"]), p.get("branch")), []).append(((scope, cond.key), events))
    return out


async def _read_streaks(project_id: str, ladder: LadderDef, *, primary_repo: str,
                        list_runs: ListRuns) -> dict[_Key, Reading]:
    readings: dict[_Key, Reading] = {}
    for (repo, workflow, branch), conds in _streak_conditions(ladder, primary_repo).items():
        try:
            runs = await asyncio.to_thread(list_runs, repo, workflow, branch=branch, limit=RUNS_LIMIT)
        except Exception as exc:
            # A missing workflow is a definition problem the view shows; not an incident.
            log.info("milestone_refresh.runs_failed project=%s workflow=%s: %s", project_id, workflow, exc)
            for key, _events in conds:
                readings[key] = _error(f"workflow runs read failed: {exc}")
            continue
        for key, events in conds:
            count, at, url = streak_from_runs([r for r in runs if r.get("event") in events])
            readings[key] = Reading({"n": count}, at, None, url)
    return readings


# ---------------------------------------------------------------- rows


@dataclass(frozen=True)
class _Prev:
    value: dict | None
    measured_at: datetime | None
    source_url: str | None


@dataclass(frozen=True)
class _Write:
    status: str
    value: dict | None
    target: dict | None
    measured_at: datetime | None
    error: str | None
    source_url: str | None


def _target(measure: Measure) -> dict | None:
    if measure.op is None:
        return None
    return {"op": measure.op, "target": measure.target}


def _final(prev: _Prev | None, reading: Reading, status: str, target: dict | None) -> _Write:
    """Apply the error rule: a failed reading keeps the previous value (unless it
    carries a partial one) and always the previous ``measured_at``."""
    if reading.error is None:
        return _Write(status, reading.value, target, reading.measured_at, None, reading.source_url)
    return _Write(
        "unknown",
        reading.value if reading.value is not None else (prev.value if prev else None),
        target,
        prev.measured_at if prev else None,
        reading.error,
        reading.source_url or (prev.source_url if prev else None),
    )


def _leaves_reading(sources: Iterable[tuple[str, _Key]], readings: Mapping[_Key, Reading],
                    leaves: Mapping[str, list[dict]], now: datetime) -> Reading:
    items: dict[str, dict] = {}
    errors: list[str] = []
    ok = 0
    for ref_key, reading_key in sources:
        r = readings.get(reading_key)
        if r is None or r.error is not None:
            errors.append(f"{ref_key}: {r.error if r else 'not read'}")
        else:
            ok += 1
        for item in leaves.get(ref_key, []):
            items.setdefault(f"{item.get('repo')}#{item.get('number')}", item)
    merged = list(items.values())
    value = {"done": leaves_done(merged), "total": len(merged), "items": merged}
    if not errors:
        return Reading(value, now, None, None)
    return Reading(value if ok else None, None, "; ".join(errors), None)


def _plan(ladder: LadderDef, readings: Mapping[_Key, Reading], leaves: Mapping[str, list[dict]],
          prev: Mapping[_Key, _Prev], now: datetime) -> dict[_Key, _Write]:
    writes: dict[_Key, _Write] = {}
    for scope, ms, cond in ladder_conditions(ladder):
        m = cond.measure
        reading: Reading
        if m.kind in ("manual", "ladder_leaves"):
            continue
        if m.kind == "ci_metric":
            reading = _error(CI_METRIC_PHASE1_ERROR)
        elif m.kind == "sub_issues":
            epic = sub_issues_epic(cond, ms)
            reading = (readings.get(epic_reading_key(epic)) or _error("not read")) if epic else _error("no epic to read")
        else:
            reading = readings.get((scope, cond.key)) or _error("not read")
        key = (scope, cond.key)
        writes[key] = _final(prev.get(key), reading, evaluate(m, reading), _target(m))

    leaves_writes: list[tuple[str, _Write]] = []
    for ms in ladder.milestones:
        sources = [(e.key, epic_reading_key(e)) for e in ms.epics] + [(r.key, ref_reading_key(r)) for r in ms.refs]
        reading = _leaves_reading(sources, readings, leaves, now)
        key = (leaves_scope(ms.key), LEAVES_KEY)
        w = _final(prev.get(key), reading, "info", None)
        writes[key] = w
        leaves_writes.append((ms.key, w))

    for cond in ladder.numbers:
        if cond.measure.kind != "ladder_leaves":
            continue
        values = [w.value for _, w in leaves_writes if w.value is not None]
        done = sum(int(v.get("done") or 0) for v in values)
        total = sum(int(v.get("total") or 0) for v in values)
        # The oldest leaves measurement; unknown if any leaves row has none.
        times = [w.measured_at for _, w in leaves_writes]
        known = [t for t in times if t is not None]
        measured_at = now if not times else (min(known) if len(known) == len(times) else None)
        errors = [f"{k}: {w.error}" for k, w in leaves_writes if w.error]
        reading = Reading({"done": done, "total": total}, measured_at, "; ".join(errors) or None, None)
        status = evaluate(cond.measure, reading)
        # Sums of kept leaf values: write them even when a leaves row errored.
        writes[("numbers", cond.key)] = _Write(status, reading.value, None, measured_at, reading.error, None)
    return writes


# ---------------------------------------------------------------- DB


async def _load(project_id: str) -> tuple[str, LadderDef, str | None, dict[_Key, _Prev]] | None:
    async with db_session() as db:
        ladder = (await db.execute(
            select(MilestoneLadder).where(MilestoneLadder.project_id == project_id)
        )).scalar_one_or_none()
        if ladder is None or not ladder.definition:
            return None
        primary = (await db.execute(
            select(ProjectRepo.repo).where(
                ProjectRepo.project_id == project_id, ProjectRepo.is_primary.is_(True))
        )).scalars().first()
        rows = (await db.execute(
            select(MilestoneMeasurement).where(MilestoneMeasurement.ladder_id == ladder.id)
        )).scalars().all()
        # Naive UTC throughout: Postgres reads these timestamptz columns back aware.
        prev = {(r.scope, r.key): _Prev(
            r.value, _naive_utc(r.measured_at) if r.measured_at else None, r.source_url)
            for r in rows}
        return ladder.id, from_json(ladder.definition), primary, prev


async def _persist(ladder_id: str, writes: Mapping[_Key, _Write], now: datetime) -> None:
    async with db_session() as db:
        existing = {(r.scope, r.key): r for r in (await db.execute(
            select(MilestoneMeasurement).where(MilestoneMeasurement.ladder_id == ladder_id)
        )).scalars().all()}
        for (scope, key), w in writes.items():
            row = existing.get((scope, key))
            if row is None:
                row = MilestoneMeasurement(ladder_id=ladder_id, scope=scope, key=key)
                db.add(row)
            row.status = w.status
            row.value = w.value
            row.target = w.target
            row.measured_at = w.measured_at
            row.fetched_at = now
            row.error = w.error
            row.source_url = w.source_url
        await db.flush()


async def refresh_ladder(project_id: str, *, graphql: GraphQL, list_runs: ListRuns, now: datetime,
                         dry_run: bool = False) -> RefreshResult:
    """Read every GitHub-backed condition of the product's ladder and upsert its rows
    (``dry_run``: compute and count, write nothing). ``graphql(owner, query)`` is called
    with the primary repo's owner and must return per-alias errors in the payload
    (``partial_ok``); ``list_runs(repo, workflow, *, branch, limit)`` runs off-loop."""
    now = _naive_utc(now)
    loaded = await _load(project_id)
    if loaded is None:
        return RefreshResult(0, 0)
    ladder_id, ladder, primary_repo, prev = loaded
    if not primary_repo:
        log.info("milestone_refresh.no_primary_repo project=%s", project_id)
        return RefreshResult(0, 0)
    readings, leaves = await _read_graphql(project_id, ladder, primary_repo=primary_repo,
                                           graphql=graphql, now=now)
    readings.update(await _read_streaks(project_id, ladder, primary_repo=primary_repo,
                                        list_runs=list_runs))
    writes = _plan(ladder, readings, leaves, prev, now)
    if not dry_run:
        await _persist(ladder_id, writes, now)
    return RefreshResult(len(writes), sum(1 for w in writes.values() if w.error is not None))


async def newest_fetched_at(project_id: str) -> datetime | None:
    """When the product's ladder was last refreshed (its newest ``fetched_at``)."""
    async with db_session() as db:
        return (await db.execute(
            select(func.max(MilestoneMeasurement.fetched_at))
            .join(MilestoneLadder, MilestoneLadder.id == MilestoneMeasurement.ladder_id)
            .where(MilestoneLadder.project_id == project_id)
        )).scalar_one_or_none()


async def refresh_if_due(project_id: str, *, force: bool, graphql: GraphQL, list_runs: ListRuns,
                         now: datetime, dry_run: bool = False) -> RefreshResult | None:
    """Refresh unless the ladder was fetched within ``REFRESH_MIN_INTERVAL_S`` (``force``,
    an on-demand refresh) or ``STALE_AFTER_S`` (otherwise). ``None``: not due."""
    newest = await newest_fetched_at(project_id)
    window = timedelta(seconds=REFRESH_MIN_INTERVAL_S if force else STALE_AFTER_S)
    if newest is not None and _naive_utc(now) - _naive_utc(newest) < window:
        return None
    return await refresh_ladder(project_id, graphql=graphql, list_runs=list_runs, now=now,
                                dry_run=dry_run)


async def list_ladder_fetch_times() -> list[tuple[str, datetime | None]]:
    """``(project_id, newest fetched_at or None)`` for every ladder."""
    async with db_session() as db:
        rows = (await db.execute(
            select(MilestoneLadder.project_id, func.max(MilestoneMeasurement.fetched_at))
            .outerjoin(MilestoneMeasurement, MilestoneMeasurement.ladder_id == MilestoneLadder.id)
            .group_by(MilestoneLadder.project_id)
        )).all()
    return [(pid, fetched) for pid, fetched in rows]


async def list_refresh_requests() -> dict[str, datetime]:
    """``project_id -> refresh_requested_at`` for every ladder with a request stamped."""
    async with db_session() as db:
        rows = (await db.execute(
            select(MilestoneLadder.project_id, MilestoneLadder.refresh_requested_at)
            .where(MilestoneLadder.refresh_requested_at.is_not(None))
        )).all()
    return {pid: at for pid, at in rows}
