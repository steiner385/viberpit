"""PM Sync — maintains the WorkItem mirror + Feature index (spec §4).

Same two-object mold as SdlcIntake: ``PmSync`` is the Automation contract
surface; ``PmSyncLoop`` is the supervised control loop with every I/O seam
injected. Sync rules: backfill is resumable (cursor saved per page), the
incremental watermark advances ONLY after a fully successful pass, and one
repo's failure never stalls the others.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

from server.pm.feature_index import (
    FeatureIndexPlan, FeatureRowView, plan_feature_index)
from server.pm.featuredoc import FeatureDoc, FeatureDocError, parse_feature_doc
from server.pm.github_sync import (
    WorkItemView, build_issues_query, build_prs_query, parse_page)

from .base import AutomationStatus, ConfigField

log = logging.getLogger(__name__)

# Bound one tick's backfill work so a huge history spreads across ticks
# (the saved cursor makes each continuation cheap).
_MAX_PAGES_PER_TICK = 20


@dataclass(frozen=True)
class RepoSyncView:
    project_id: str
    project_slug: str
    repo: str
    is_primary: bool
    backfill_done: bool
    backfill_phase: str          # "issues" | "prs"
    backfill_cursor: str | None
    watermark: datetime | None   # aware UTC
    last_synced_at: datetime | None
    sync_requested_at: datetime | None


class PmSync:
    id = "pm-sync"
    name = "PM Sync"
    description = (
        "Mirrors onboarded projects' GitHub issues/PRs into the work-item index "
        "and refreshes the feature index from merged docs. Read-only toward "
        "GitHub; the mirror is disposable, GitHub stays the truth.")
    modes: tuple[str, ...] = ("off", "on")
    config_schema = [
        ConfigField("min_sync_interval_s", "Min sync interval (s)", "int", 300,
                    help="A repo is re-synced no more often than this unless "
                         "sync-now is requested."),
    ]

    def status(self, ctx: Any) -> AutomationStatus:
        counts = getattr(ctx, "pm_sync_counts", None) if ctx else None
        if not counts:
            return AutomationStatus(text="idle", level="idle")
        text = (f"{counts.get('synced', 0)} synced · "
                f"{counts.get('backfilling', 0)} backfilling · "
                f"{counts.get('errors', 0)} errors")
        level = "error" if counts.get("errors") else (
            "active" if counts.get("backfilling") else "idle")
        return AutomationStatus(text=text, level=level, counts=counts)

    async def on_state_change(self, *, enabled: bool, mode: str | None,
                              config: dict, ctx: Any) -> None:
        return None


ReadConfig = Callable[[], Awaitable[tuple[str, dict]]]
ListRepos = Callable[[], Awaitable[list[RepoSyncView]]]
Fetch = Callable[[str], Awaitable[dict]]
UpsertItems = Callable[[list[WorkItemView]], Awaitable[None]]
SaveSyncState = Callable[..., Awaitable[None]]
ReadFeatureDocs = Callable[[str], Awaitable[list[tuple[str, str]]]]
ListFeatureRows = Callable[[str], Awaitable[list[FeatureRowView]]]
ApplyFeaturePlan = Callable[[str, FeatureIndexPlan, dict[str, FeatureDoc]], Awaitable[None]]


class PmSyncLoop:
    """One ``tick()`` syncs every due repo: backfill pages (resumable) or an
    incremental pass, then the feature index for the project's primary repo."""

    def __init__(
        self,
        ctx: Any,
        *,
        read_config: ReadConfig,
        list_repos: ListRepos,
        fetch: Fetch,
        upsert_items: UpsertItems,
        save_sync_state: SaveSyncState,
        read_feature_docs: ReadFeatureDocs,
        list_feature_rows: ListFeatureRows,
        apply_feature_plan: ApplyFeaturePlan,
        now_utc: Callable[[], datetime],
    ) -> None:
        self._ctx = ctx
        self._read_config = read_config
        self._list_repos = list_repos
        self._fetch = fetch
        self._upsert_items = upsert_items
        self._save_sync_state = save_sync_state
        self._read_feature_docs = read_feature_docs
        self._list_feature_rows = list_feature_rows
        self._apply_feature_plan = apply_feature_plan
        self._now_utc = now_utc

    async def tick(self) -> None:
        """One control-loop iteration. Best-effort: swallows exceptions so a
        transient failure never crashes the supervising driver."""
        try:
            await self._run()
        except Exception:  # pragma: no cover — belt+braces, mirrors the mold
            log.warning("pm_sync.tick_error", exc_info=True)

    async def _run(self) -> None:
        mode, config = await self._read_config()
        if mode == "off":
            self._stash(0, 0, 0)
            return
        interval = timedelta(seconds=int(config.get("min_sync_interval_s", 300)))
        now = self._now_utc()
        synced = backfilling = errors = 0
        for view in await self._list_repos():
            due = (view.sync_requested_at is not None
                   or view.last_synced_at is None
                   or now - view.last_synced_at >= interval
                   or not view.backfill_done)
            if not due:
                continue
            try:
                if view.backfill_done:
                    await self._incremental(view, now)
                    synced += 1
                else:
                    done = await self._backfill(view, now)
                    if done:
                        synced += 1
                    else:
                        backfilling += 1
            except Exception:
                errors += 1
                await self._ctx.emit(
                    "warning", f"sync failed for {view.repo}",
                    kind="sync_error", target=view.repo)
                log.warning("pm_sync.repo_error repo=%s", view.repo, exc_info=True)
        self._stash(synced, backfilling, errors)

    async def _backfill(self, view: RepoSyncView, now: datetime) -> bool:
        """Resumable full-history import: issues first, then PRs. Saves the
        cursor after every page; returns True when both phases complete."""
        phase = view.backfill_phase or "issues"
        cursor = view.backfill_cursor
        max_seen: datetime | None = None
        for _ in range(_MAX_PAGES_PER_TICK):
            if phase == "issues":
                payload = await self._fetch(build_issues_query(view.repo, cursor, None))
                items, cursor, more = parse_page(payload, view.repo, "issue")
            else:
                payload = await self._fetch(build_prs_query(view.repo, cursor))
                items, cursor, more = parse_page(payload, view.repo, "pr")
            if items:
                await self._upsert_items(items)
                for item in items:
                    if item.gh_updated_at and (max_seen is None or item.gh_updated_at > max_seen):
                        max_seen = item.gh_updated_at
            if not more:
                if phase == "issues":
                    phase, cursor = "prs", None
                    await self._save_sync_state(
                        view, backfill_phase=phase, backfill_cursor=None)
                    continue
                # Both phases exhausted — backfill complete.
                await self._save_sync_state(
                    view, backfill_done=True, backfill_cursor=None,
                    backfill_phase="prs", watermark=max_seen or now,
                    last_synced_at=now, sync_requested_at=None)
                await self._feature_index(view)
                await self._ctx.emit("info", f"backfill complete for {view.repo}",
                                     kind="backfill_done", target=view.repo)
                return True
            await self._save_sync_state(
                view, backfill_phase=phase, backfill_cursor=cursor)
        return False  # page budget exhausted; cursor saved, continues next tick

    async def _incremental(self, view: RepoSyncView, now: datetime) -> None:
        """One incremental pass. State is saved ONCE, after full success —
        a failure mid-pass leaves the watermark untouched (at-least-once)."""
        max_seen = view.watermark
        since = view.watermark.isoformat() if view.watermark else None
        cursor: str | None = None
        while True:  # issues: server-side `since` filter does the bounding
            payload = await self._fetch(build_issues_query(view.repo, cursor, since))
            items, cursor, more = parse_page(payload, view.repo, "issue")
            if items:
                await self._upsert_items(items)
                for item in items:
                    if item.gh_updated_at and (max_seen is None or item.gh_updated_at > max_seen):
                        max_seen = item.gh_updated_at
            if not more:
                break
        cursor = None
        while True:  # PRs: newest-first, stop once nodes fall behind the watermark
            payload = await self._fetch(build_prs_query(view.repo, cursor))
            items, cursor, more = parse_page(payload, view.repo, "pr")
            fresh = [i for i in items
                     if view.watermark is None
                     or (i.gh_updated_at and i.gh_updated_at > view.watermark)]
            if fresh:
                await self._upsert_items(fresh)
                for item in fresh:
                    if item.gh_updated_at and (max_seen is None or item.gh_updated_at > max_seen):
                        max_seen = item.gh_updated_at
            if not more or len(fresh) < len(items):
                break
        await self._feature_index(view)
        await self._save_sync_state(
            view, watermark=max_seen or now, last_synced_at=now,
            sync_requested_at=None)

    async def _feature_index(self, view: RepoSyncView) -> None:
        if not view.is_primary:
            return
        docs: list[tuple[str, FeatureDoc]] = []
        by_slug: dict[str, FeatureDoc] = {}
        for path, text in await self._read_feature_docs(view.repo):
            try:
                doc = parse_feature_doc(text)
            except FeatureDocError:
                log.warning("pm_sync.bad_feature_doc repo=%s path=%s",
                            view.repo, path, exc_info=True)
                continue  # one bad doc never poisons the index (spec §4)
            docs.append((path, doc))
            by_slug[doc.slug] = doc
        rows = await self._list_feature_rows(view.project_id)
        plan = plan_feature_index(rows, docs)
        await self._apply_feature_plan(view.project_id, plan, by_slug)

    def _stash(self, synced: int, backfilling: int, errors: int) -> None:
        self._ctx.pm_sync_counts = {
            "synced": synced, "backfilling": backfilling, "errors": errors}
