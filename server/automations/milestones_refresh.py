"""Milestones Refresh (spec 2026-11-05 §8): keeps every product's release-milestone
measurements current.

The loop wakes every ``TICK_S`` and refreshes, one ladder at a time, each ladder whose
newest fetch is ``STALE_AFTER_S`` old (or absent), or whose ``refresh_requested_at``
(stamped by the on-demand refresh) is newer than it. A ladder it attempted without a
fetch landing (a failure, or a dry run) waits a full interval or a newer request, so a
broken ladder is not re-read every tick. The service is ``server/pm/milestone_refresh.py``.

Modes: ``off`` (no refresh), ``dry_run`` (fetch and log, write nothing), ``on``.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from server.automations.base import AutomationStatus
from server.automations.context import observation_failed, publish_observation
from server.automations.engine_policy import EnginePolicy
from server.github.host_failures import log_gh_failure
from server.pm.milestone_refresh import STALE_AFTER_S, RefreshResult
from server.time_iso import aware_utc

log = logging.getLogger(__name__)

AUTOMATION_ID = "milestones-refresh"
TICK_S = 15.0
MODES: tuple[str, ...] = ("off", "dry_run", "on")


class MilestonesRefresh:
    id = AUTOMATION_ID
    name = "Milestones Refresh"
    work_class = "maintenance"
    description = ("Keeps each product's release-milestone gates current: every 10 minutes, "
                   "or at once when someone asks for a refresh, reads the GitHub searches, "
                   "issue and PR states, epic sub-issues, workflow streaks and CI-published "
                   "metrics the ladder names, and records each reading with its time.")
    modes: tuple[str, ...] = MODES
    engine_policy: EnginePolicy = EnginePolicy.CLAUDE_ONLY
    config_schema: list = []

    def status(self, ctx: Any) -> AutomationStatus:
        counts = getattr(ctx, "milestones_refresh_counts", None) if ctx is not None else None
        if not counts:
            return AutomationStatus(text="idle", level="idle")
        text = (f"{counts['refreshed']} refreshed · {counts['dryRun']} dry-run · "
                f"{counts['failed']} failed")
        busy = counts["refreshed"] or counts["dryRun"]
        return AutomationStatus(text=text, level="active" if busy else "idle",
                                counts=dict(counts))

    async def on_state_change(self, *, enabled: bool, mode: str | None, config: dict,
                              ctx: Any) -> None:
        ...  # the loop reads its mode each tick


async def read_config() -> tuple[str, dict]:
    """The automation row's ``(effective mode, config)``: ``off`` unless the row is
    enabled; an enabled row with no explicit mode runs ``on``."""
    from server.automations import config_store
    active = tuple(m for m in MilestonesRefresh.modes if m != "off")
    return await config_store.read_automation_config(
        MilestonesRefresh, modes=active, default_mode="on", log_key="milestones_refresh")


class MilestonesRefreshLoop:
    def __init__(self, ctx: Any, *,
                 read_config: Callable[[], Awaitable[tuple[str, dict]]],
                 list_ladder_projects: Callable[[], Awaitable[list[tuple[str, datetime | None]]]],
                 list_requested: Callable[[], Awaitable[dict[str, datetime]]],
                 refresh: Callable[..., Awaitable[RefreshResult]],
                 now: Callable[[], datetime] | None = None) -> None:
        self._ctx = ctx
        self._read_config = read_config
        #: ``(project_id, newest fetched_at or None)`` per ladder.
        self._list_ladder_projects = list_ladder_projects
        #: ``project_id -> refresh_requested_at``.
        self._list_requested = list_requested
        #: ``refresh(project_id, *, dry_run)``.
        self._refresh = refresh
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._lock = asyncio.Lock()
        #: project_id -> when this loop last attempted it (aware UTC).
        self._attempted: dict[str, datetime] = {}

    async def tick(self) -> None:
        async with self._lock:
            try:
                await self._run_once()
            except Exception:
                observation_failed(self._ctx)
                log.warning("milestones_refresh.tick_failed", exc_info=True)

    def _due(self, project_id: str, fetched: datetime | None, requested: datetime | None,
             now: datetime) -> bool:
        marks = [t for t in (aware_utc(fetched), self._attempted.get(project_id)) if t is not None]
        last = max(marks) if marks else None
        if last is None or now - last >= timedelta(seconds=STALE_AFTER_S):
            return True
        req = aware_utc(requested)
        return req is not None and req > last

    async def _run_once(self) -> None:
        mode, _config = await self._read_config()
        if mode == "off":
            return
        dry_run = mode == "dry_run"
        now = aware_utc(self._now())
        assert now is not None
        ladders = await self._list_ladder_projects()
        requested = await self._list_requested()
        counts = {"refreshed": 0, "failed": 0, "dryRun": 0}
        for project_id, fetched in ladders:
            if not self._due(project_id, fetched, requested.get(project_id), now):
                continue
            self._attempted[project_id] = now
            try:
                result = await self._refresh(project_id, dry_run=dry_run)
            except Exception as exc:
                counts["failed"] += 1
                # One constant message per product: a failing ladder is one condition.
                log_gh_failure(log, str(exc), "milestones_refresh.ladder_failed project=%s",
                               project_id, demote_edge_failures=True)
                log.info("milestones_refresh.ladder_failed_detail project=%s", project_id,
                         exc_info=True)
                continue
            if dry_run:
                counts["dryRun"] += 1
                log.info("milestones_refresh.dry_run project=%s rows=%d errors=%d",
                         project_id, result.written, result.errors)
            else:
                counts["refreshed"] += 1
        known = {pid for pid, _ in ladders}
        for pid in [p for p in self._attempted if p not in known]:
            del self._attempted[pid]
        publish_observation(self._ctx, "milestones_refresh_counts", counts)
