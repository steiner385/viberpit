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
from server.pm.gh_projects import (
    GhProjectItemView, GhProjectView, IssueRelView,
    build_issue_relations_query, build_owner_projects_query,
    build_project_fields_query, build_project_items_query,
    parse_issue_relations, parse_project_fields, parse_project_items,
    parse_viewer_projects, project_node_missing)
from server.pm.github_sync import (
    WorkItemView, build_issues_query, build_prs_query, parse_page)
from server.pm.mirror_store import ProductSyncView

from .base import AutomationStatus, ConfigField

log = logging.getLogger(__name__)

NowUtc = Callable[[], datetime]

# Bound one tick's backfill work so a huge history spreads across ticks
# (the saved cursor makes each continuation cheap).
_MAX_PAGES_PER_TICK = 20

# The Projects v2 + relations pass is far pricier than the work-item mirror
# (~high-hundreds of GraphQL points/hr if run every tick), so it is gated behind
# its own interval (issue #973). A persistently failing owner is backed off this
# long before it is retried, so a missing-credential owner is probed ~4×/hr, not
# every tick — and its warning is emitted once per failure onset, not repeated.
_OWNER_ERROR_BACKOFF = timedelta(minutes=15)


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
        ConfigField("projects_sync_interval_s", "Projects v2 sync interval (s)",
                    "int", 300,
                    help="The Projects v2 + issue-relations mirror pass runs no "
                         "more often than this (it is far pricier than the "
                         "work-item mirror); a sync-now request bypasses it."),
    ]

    def status(self, ctx: Any) -> AutomationStatus:
        counts = getattr(ctx, "pm_sync_counts", None) if ctx else None
        if not counts:
            return AutomationStatus(text="idle", level="idle")
        text = (f"{counts.get('synced', 0)} synced · "
                f"{counts.get('backfilling', 0)} backfilling · "
                f"{counts.get('gh_projects', 0)} projects · "
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

# --- Projects v2 + relations mirror seams (003 Phase A) ---------------------
# All default to None so the mirror pass is a no-op unless the orchestrator
# wires them — keeping every pre-existing PmSyncLoop test green unchanged.
ListProducts = Callable[[], Awaitable[list[ProductSyncView]]]
# Owner-aware project fetch (args: owner, query). Rooted at ``user(login:)`` and
# authenticated with the owner's GitHub App installation token so it can read
# Projects v2, which the ambient ``gh`` credential lacks scope for (Phase 0).
FetchGhProjects = Callable[[str, str], Awaitable[dict]]
SaveGhProject = Callable[[str, GhProjectView], Awaitable[str]]  # -> gh_project db id
SaveProjectFields = Callable[[str, dict], Awaitable[None]]
SaveProjectItems = Callable[[str, list[GhProjectItemView]], Awaitable[None]]
SaveRelations = Callable[[str, list[IssueRelView]], Awaitable[None]]


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
        now_utc: NowUtc,
        list_products: ListProducts | None = None,
        fetch_gh_projects: FetchGhProjects | None = None,
        save_gh_project: SaveGhProject | None = None,
        save_project_fields: SaveProjectFields | None = None,
        save_project_items: SaveProjectItems | None = None,
        save_relations: SaveRelations | None = None,
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
        # Projects v2 + relations mirror seams (003 Phase A); None ⇒ skip pass.
        self._list_products = list_products
        self._fetch_gh_projects = fetch_gh_projects
        self._save_gh_project = save_gh_project
        self._save_project_fields = save_project_fields
        self._save_project_items = save_project_items
        self._save_relations = save_relations
        # Projects v2 pass gating (issue #973). In-memory on the long-lived loop:
        # ``_projects_next_run`` is the earliest UTC time the pass may run again;
        # ``_last_gh_projects`` keeps the status count steady across skipped ticks;
        # ``_owner_next_retry`` / ``_owner_failing`` back off + dedup per-owner
        # discovery failures so a missing-credential owner doesn't warn every tick.
        self._projects_next_run: datetime | None = None
        self._last_gh_projects: int = 0
        self._owner_next_retry: dict[str, datetime] = {}
        self._owner_failing: set[str] = set()

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
            self._stash(0, 0, 0, 0)
            return
        interval = timedelta(seconds=int(config.get("min_sync_interval_s", 300)))
        now = self._now_utc()
        synced = backfilling = errors = 0
        force_projects = False
        for view in await self._list_repos():
            if view.sync_requested_at is not None:
                force_projects = True  # a sync-now request bypasses the gate below
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
                    "warn", f"sync failed for {view.repo}",
                    kind="sync_error", target=view.repo)
                log.warning("pm_sync.repo_error repo=%s", view.repo, exc_info=True)
        gh_projects = await self._sync_projects_v2(now, config, force_projects)
        self._stash(synced, backfilling, errors, gh_projects)

    async def _sync_projects_v2(self, now: datetime, config: dict,
                                force: bool) -> int:
        """Best-effort Projects v2 + issue-relations mirror pass.

        Discovery is owner-aware: under the GitHub App installation token
        ``viewer`` resolves to the App bot, and ``user(login: owner)`` 404s on
        an Organization owner, so we root the query at
        ``repositoryOwner(login: owner)`` (resolves both Users and
        Organizations) and iterate the DISTINCT owners of the products'
        repos, deduping discovered projects by node_id across owners. All
        project-scoped fetches ride the owner-aware ``fetch_gh_projects`` seam
        (App token, has ``read:project``); relations are repo-scoped and keep
        using the plain ``fetch`` seam (ambient credential has repo scope).

        Returns the count of products linked to a GhProject this tick. A missing
        ``list_products`` seam (or any other mirror seam) makes this a no-op — the
        seams default to None so pre-existing loops skip the pass entirely.
        Per-product failures are isolated and emitted like per-repo errors."""
        if not (self._list_products and self._fetch_gh_projects
                and self._save_gh_project and self._save_project_fields
                and self._save_project_items and self._save_relations):
            return 0
        # Interval short-circuit (issue #973): skip the whole pass — discovery,
        # project mirror AND relations — until the interval elapses, unless a
        # sync-now request forces it. Return the last count so status holds steady.
        if not force and self._projects_next_run is not None \
                and now < self._projects_next_run:
            return self._last_gh_projects
        interval = timedelta(
            seconds=int(config.get("projects_sync_interval_s", 300)))
        self._projects_next_run = now + interval
        products = await self._list_products()
        owners = {r.split("/", 1)[0] for p in products for r in p.repos
                  if "/" in r}
        discovered: dict[str, GhProjectView] = {}  # node_id -> view (deduped)
        for owner in sorted(owners):
            # Per-owner failure backoff: a persistently failing owner is skipped
            # silently until its retry time, so it never re-warns every tick.
            retry_at = self._owner_next_retry.get(owner)
            if retry_at is not None and now < retry_at:
                continue
            try:
                cursor: str | None = None
                for _ in range(_MAX_PAGES_PER_TICK):
                    payload = await self._fetch_gh_projects(
                        owner, build_owner_projects_query(owner, cursor))
                    views, cursor, more = parse_viewer_projects(payload)
                    for view in views:
                        discovered.setdefault(view.node_id, view)
                    if not more:
                        break
                self._owner_next_retry.pop(owner, None)  # recovered
                self._owner_failing.discard(owner)
            except Exception:
                self._owner_next_retry[owner] = now + _OWNER_ERROR_BACKOFF
                if owner not in self._owner_failing:  # warn on onset only (dedup)
                    self._owner_failing.add(owner)
                    await self._ctx.emit(
                        "warn", f"project discovery failed for {owner}",
                        kind="gh_projects_error", target=owner)
                log.warning("pm_sync.project_discovery_error owner=%s",
                            owner, exc_info=True)
        by_title: dict[str, GhProjectView] = {
            v.title.lower(): v for v in discovered.values()}
        linked = 0
        for product in products:
            try:
                linked += await self._sync_one_product(product, by_title)
            except Exception:
                await self._ctx.emit(
                    "warn", f"project sync failed for {product.project_id}",
                    kind="gh_projects_error", target=product.project_id)
                log.warning("pm_sync.project_error project=%s",
                            product.project_id, exc_info=True)
        self._last_gh_projects = linked
        return linked

    async def _sync_one_product(
        self, product: ProductSyncView,
        by_title: dict[str, GhProjectView],
    ) -> int:
        """Sync one product's project (fields + items) and its repos' relations.
        Returns 1 if the product is linked to a GhProject, else 0."""
        assert self._fetch_gh_projects and self._save_gh_project
        assert self._save_project_fields and self._save_project_items
        assert self._save_relations
        view = (by_title.get((product.slug or "").lower())
                or by_title.get((product.name or "").lower()))
        gh_db_id = product.gh_project_db_id
        node_id = product.gh_node_id
        if view is not None:
            gh_db_id = await self._save_gh_project(product.project_id, view)
            node_id = view.node_id
        linked = 1 if (gh_db_id and node_id) else 0
        if gh_db_id and node_id:
            owner = _owner_of(product)
            fields = await self._fetch_gh_projects(
                owner, build_project_fields_query(node_id))
            # Board DELETED on GitHub (node(id:) → null) vs. merely empty: a
            # deletion must NOT prune the mirror silently (spec §10, issue #973).
            # Halt the board mirror loudly (needs human) and leave the rows intact
            # so a human can decide; relations below are board-independent.
            if project_node_missing(fields):
                await self._emit_board_deleted(product, node_id)
                linked = 0
            else:
                await self._save_project_fields(
                    gh_db_id, parse_project_fields(fields))
                items: list[GhProjectItemView] = []
                cursor: str | None = None
                deleted = False
                for _ in range(_MAX_PAGES_PER_TICK):
                    payload = await self._fetch_gh_projects(
                        owner, build_project_items_query(node_id, cursor))
                    if project_node_missing(payload):  # deleted mid-tick
                        deleted = True
                        break
                    page, cursor, more = parse_project_items(payload)
                    items.extend(page)
                    if not more:
                        break
                if deleted:
                    await self._emit_board_deleted(product, node_id)
                    linked = 0
                else:
                    await self._save_project_items(gh_db_id, items)
        # Relations are repo-scoped (ambient credential): always run per repo,
        # independent of whether the project itself linked.
        for repo in product.repos:
            rels: list[IssueRelView] = []
            cursor = None
            for _ in range(_MAX_PAGES_PER_TICK):
                payload = await self._fetch(
                    build_issue_relations_query(repo, cursor))
                page_r, cursor, more = parse_issue_relations(payload, repo)
                rels.extend(page_r)
                if not more:
                    break
            await self._save_relations(repo, rels)
        return linked

    async def _emit_board_deleted(self, product: ProductSyncView,
                                  node_id: str) -> None:
        """Loud needs-human halt for a board deleted on GitHub (spec §10). Emitted
        at ``error`` level so the automation status flips to ``error`` rather than
        silently pruning every mirrored item."""
        await self._ctx.emit(
            "error",
            f"Projects v2 board deleted on GitHub for {product.project_id} "
            f"(node {node_id}); mirror halted, items preserved — needs human.",
            kind="gh_project_deleted", target=product.project_id)
        log.error("pm_sync.project_board_deleted project=%s node=%s",
                  product.project_id, node_id)

    async def _backfill(self, view: RepoSyncView, now: datetime) -> bool:
        """Resumable full-history import: issues first, then PRs. Saves the
        cursor after every page; returns True when both phases complete."""
        phase = view.backfill_phase or "issues"
        cursor = view.backfill_cursor
        # max_seen resets to None each tick intentionally: a backfill spanning
        # >_MAX_PAGES_PER_TICK ticks derives its final watermark from only the
        # last tick's pages.  This is safe — any under-set watermark is
        # self-healing: the first incremental pass idempotently re-fetches the
        # missed window.  Do NOT accumulate across ticks (would require
        # persisting an intermediate watermark, adding complexity for no gain).
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
        # The read is atomic-or-raise: a partial/untrustworthy read raises rather
        # than returning a subset. Skip the reindex on failure — running the plan
        # over an incomplete doc set would ORPHAN the docs the read missed (the
        # dogfood bug). Best-effort and decoupled from work-item sync: a failed
        # read never propagates out to abort the surrounding incremental pass.
        try:
            raw = await self._read_feature_docs(view.repo)
        except Exception:
            log.warning("pm_sync.feature_read_failed repo=%s — skipping reindex",
                        view.repo, exc_info=True)
            await self._ctx.emit(
                "warn", f"feature-doc read failed for {view.repo}; reindex skipped",
                kind="feature_read_error", target=view.repo)
            return
        docs: list[tuple[str, FeatureDoc]] = []
        by_slug: dict[str, FeatureDoc] = {}
        for path, text in raw:
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

    def _stash(self, synced: int, backfilling: int, errors: int,
               gh_projects: int = 0) -> None:
        self._ctx.pm_sync_counts = {
            "synced": synced, "backfilling": backfilling, "errors": errors,
            "gh_projects": gh_projects}


def _owner_of(product: ProductSyncView) -> str:
    """The owner login for a product's project fetches — the owner of its first
    ``owner/name`` repo. Products in this codebase are single-owner, so any repo
    yields the same owner; the first repo is the stable choice."""
    for repo in product.repos:
        if "/" in repo:
            return repo.split("/", 1)[0]
    return ""
