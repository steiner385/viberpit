"""GitHub reconciler (spec 003 §6): converge Projects v2 fields, membership and native
blocked-by edges toward the decision layer's desired state. Leader-gated at task creation
(orchestrator), SHA-fenced and kill-switched per cycle, write-ahead journaled, blast-capped,
rate-budgeted, no blind retry.

Rate budget: GitHub's ``rateLimit`` is a Query-root field and cannot ride along in a mutation
document, so the budget is sampled ONCE per cycle with ``build_rate_limit_query()`` through the
same ``mutate`` seam — before any product is converged. Below ``min_rate_remaining`` the whole
cycle is skipped; a failed/None rate query is NOT a skip (we proceed and let per-batch errors
speak).
"""
from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from server.pm.desired_state import derive
from server.pm.gh_mutations import (UnknownOption, build_batch, build_rate_limit_query, chunk,
                                    parse_batch, parse_rate_limit)
from server.pm.mirror_store import ProductSyncView
from server.pm.reconcile_diff import Mutation, compile_edges, diff_fields, membership
from server.pm.reconcile_inputs import ProductFacts

from .base import AutomationStatus, ConfigField

log = logging.getLogger(__name__)

_INTERRUPTED_AFTER = timedelta(minutes=10)
_COUNT_KEYS = ("products", "planned", "applied", "failed", "parked", "skipped_stale",
               "skipped_rate", "halted_cap", "fenced", "disabled", "dry_run")

ReadConfig = Callable[[], Awaitable[tuple[str, dict]]]
ListProducts = Callable[[], Awaitable[list[ProductSyncView]]]
LoadFacts = Callable[[ProductSyncView, datetime], Awaitable[ProductFacts]]
Mutate = Callable[[str, str], Awaitable[dict]]
JournalRecord = Callable[[list[Mutation], str, datetime, bool], Awaitable[list[str]]]
JournalApplied = Callable[[list[str], datetime], Awaitable[None]]
JournalFailed = Callable[[str, str, datetime], Awaitable[None]]
JournalPark = Callable[[str, str, datetime], Awaitable[None]]
JournalFailureCount = Callable[[str], Awaitable[int]]
JournalParked = Callable[[str], Awaitable[bool]]
JournalAppliedKeys = Callable[[], Awaitable[set[str]]]
JournalRecover = Callable[[datetime], Awaitable[int]]
RequestSync = Callable[[str], Awaitable[None]]
ShaFence = Callable[[], Awaitable[bool]]
KillSwitch = Callable[[], bool]
SaveFieldCache = Callable[[str, dict], Awaitable[None]]
NowUtc = Callable[[], datetime]


def _owner_of(product: ProductSyncView) -> str:
    """Owner login for a product's GraphQL calls (same derivation as the facts loader)."""
    return product.repos[0].split("/", 1)[0] if product.repos else ""


class GhReconcile:
    id = "gh-reconcile"
    name = "GitHub Reconcile"
    description = ("Writes Projects v2 Status/Score/Stage, board membership and native blocked-by "
                   "edges from ccswitch decisions. Journaled, capped, leader-gated.")
    modes: tuple[str, ...] = ("off", "dry_run", "apply")
    config_schema = [
        ConfigField("max_mutations_per_cycle", "Blast cap (mutations per cycle)", "int", 50,
                    "Exceeding this halts the product's cycle loudly; nothing is applied."),
        ConfigField("migrate_cap", "Blast cap while migrate is on", "int", 500,
                    "Used instead of the normal cap for the one-time migration run."),
        ConfigField("migrate", "One-time migration", "bool", False,
                    "Raise the cap and bootstrap board fields. Turn off after the first apply."),
        ConfigField("min_rate_remaining", "Minimum GraphQL budget", "int", 500,
                    "Skip the cycle when the GraphQL rateLimit.remaining is below this."),
    ]

    def status(self, ctx: Any) -> AutomationStatus:
        c = getattr(ctx, "gh_reconcile_counts", None) if ctx is not None else None
        if not c:
            return AutomationStatus(text="idle", level="idle")
        if c.get("disabled"):
            return AutomationStatus(text="kill switch off (CCSWITCH_GH_RECONCILE_ENABLED)",
                                    level="idle", counts=c)
        if c.get("fenced"):
            return AutomationStatus(text="refused: daemon SHA != origin/master", level="warn",
                                    counts=c)
        parts = [f"{c.get('planned', 0)} planned", f"{c.get('applied', 0)} applied"]
        if c.get("dry_run"):
            parts.insert(0, "dry run")
        for key in ("failed", "parked", "halted_cap", "skipped_stale", "skipped_rate"):
            if c.get(key):
                parts.append(f"{c[key]} {key.replace('_', ' ')}")
        level = "error" if c.get("halted_cap") else (
            "warn" if (c.get("parked") or c.get("failed")) else
            ("active" if c.get("applied") else "idle"))
        return AutomationStatus(text=", ".join(parts), level=level, counts=c)

    async def on_state_change(self, *, enabled: bool, mode: str | None, config: dict,
                              ctx: Any) -> None:
        return None


class GhReconcileLoop:
    """One ``tick()`` converges every product once: freshness gate → desired state → diff →
    capped, journaled, batched apply."""

    def __init__(self, ctx: Any, *, read_config: ReadConfig, list_products: ListProducts,
                 load_facts: LoadFacts, mutate: Mutate, journal_record: JournalRecord,
                 journal_applied: JournalApplied, journal_failed: JournalFailed,
                 journal_park: JournalPark, journal_failure_count: JournalFailureCount,
                 journal_parked: JournalParked, journal_applied_keys: JournalAppliedKeys,
                 journal_recover: JournalRecover, request_sync: RequestSync, sha_fence: ShaFence,
                 kill_switch: KillSwitch, save_field_cache: SaveFieldCache,
                 now_utc: NowUtc) -> None:
        self._ctx = ctx
        self._read_config, self._list_products, self._load_facts = read_config, list_products, load_facts
        self._mutate = mutate
        self._j_record, self._j_applied, self._j_failed, self._j_park = (
            journal_record, journal_applied, journal_failed, journal_park)
        self._j_failure_count, self._j_parked, self._j_applied_keys, self._j_recover = (
            journal_failure_count, journal_parked, journal_applied_keys, journal_recover)
        self._request_sync, self._sha_fence, self._kill_switch = request_sync, sha_fence, kill_switch
        # save_field_cache is the seam Task 9's field bootstrap (_ensure_fields) writes through.
        self._save_field_cache, self._now = save_field_cache, now_utc
        self._fence_warned = False

    async def tick(self) -> None:
        try:
            await self._run()
        except Exception:
            log.warning("gh_reconcile.tick_failed", exc_info=True)

    def _stash(self, **counts: int) -> None:
        full = {k: 0 for k in _COUNT_KEYS}
        full.update(counts)
        setattr(self._ctx, "gh_reconcile_counts", full)

    async def _rate_ok(self, products: list[ProductSyncView], min_rate: int) -> bool:
        """Sample the GraphQL budget once per cycle. A failed query is not a skip."""
        if not products:
            return True
        try:
            payload = await self._mutate(_owner_of(products[0]), build_rate_limit_query())
            rate = parse_rate_limit(payload)
        except Exception:
            log.debug("gh_reconcile.rate_query_failed", exc_info=True)
            return True
        if rate is None:
            log.debug("gh_reconcile.rate_unknown")
            return True
        return rate.remaining >= min_rate

    async def _run(self) -> None:
        mode, config = await self._read_config()
        if mode == "off":
            return
        if not self._kill_switch():
            self._stash(disabled=1)
            return
        if not await self._sha_fence():
            self._stash(fenced=1)
            if not self._fence_warned:
                self._fence_warned = True
                await self._ctx.emit("warn", "reconcile refused: daemon SHA is not origin/master",
                                     kind="gh_reconcile_fenced")
            return
        self._fence_warned = False
        now = self._now()
        dry_run = mode == "dry_run"
        await self._j_recover(now - _INTERRUPTED_AFTER)
        cap = int(config.get("migrate_cap", 500)) if config.get("migrate") else \
            int(config.get("max_mutations_per_cycle", 50))
        min_rate = int(config.get("min_rate_remaining", 500))
        counts = {k: 0 for k in _COUNT_KEYS}
        counts["dry_run"] = int(dry_run)
        products = await self._list_products()
        # Rate budget: one Query-root sample per cycle, before anything is converged.
        # A dry run writes nothing, so it never spends (or needs) the budget.
        if not dry_run and not await self._rate_ok(products, min_rate):
            counts["skipped_rate"] = len(products)
            self._stash(**counts)
            return
        applied_keys = await self._j_applied_keys()
        for product in products:
            counts["products"] += 1
            try:
                await self._converge(product, now, dry_run, cap, applied_keys, counts)
            except Exception:
                log.warning("gh_reconcile.product_failed", exc_info=True)
                await self._ctx.emit("warn", f"reconcile failed for {product.slug}",
                                     kind="gh_reconcile_error", target=product.project_id)
        self._stash(**counts)

    async def _converge(self, product: ProductSyncView, now: datetime, dry_run: bool, cap: int,
                        applied_keys: set[str], counts: dict[str, int]) -> None:
        facts = await self._load_facts(product, now)
        if facts.project_node_id is None:
            return
        # 1. freshness gate (spec §6 step 1). A never-synced product is ALWAYS stale: its
        # empty mirror is absence of evidence, not evidence of absence, and planning against
        # it would "add" every open issue to a board that may already hold them.
        if facts.mirror_synced_at is None or (
                facts.newest_decision_at is not None
                and facts.mirror_synced_at < facts.newest_decision_at):
            counts["skipped_stale"] += 1
            await self._request_sync(product.project_id)
            return
        # 2. desired state + 3. diff
        planned: list[Mutation] = []
        for item in facts.items:
            mirror = facts.mirror.get(item.ref)
            if mirror is not None:
                planned.extend(diff_fields(derive(item), mirror))
        planned.extend(membership(facts.open_issue_nodes, set(facts.mirror), facts.project_node_id))
        planned.extend(compile_edges(facts.deps, facts.node_ids, applied_keys))
        keep: list[Mutation] = []
        for m in planned:
            if await self._j_parked(m.key()):
                continue
            keep.append(m)
        planned = keep
        counts["planned"] += len(planned)
        if not planned:
            return
        if len(planned) > cap:
            counts["halted_cap"] += 1
            await self._ctx.emit("error", f"reconcile halted for {product.slug}: {len(planned)} "
                                 f"mutations exceed cap {cap}; nothing applied",
                                 kind="gh_reconcile_cap", target=product.project_id,
                                 detail={"planned": len(planned), "cap": cap})
            return
        # Task 9 bootstraps missing fields/options into this cache before the batch is built.
        cache = facts.field_cache
        # 4. apply, write-ahead journaled per batch
        batch_id = f"{'dry' if dry_run else 'run'}-{now.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
        for batch in chunk(planned):
            row_ids = await self._j_record(batch, batch_id, now, dry_run)
            try:
                # build_batch is pure, so a dry run runs it too: predicting vocabulary
                # drift is exactly the job of a dry run. Only the parking of journal
                # rows is skipped — dry-run rows keep their dry_run state.
                doc, alias_map = build_batch(batch, project_node_id=facts.project_node_id,
                                             field_cache=cache)
            except UnknownOption as exc:
                if not dry_run:
                    for rid in row_ids:
                        await self._j_park(rid, f"field vocabulary drift: {exc}", now)
                counts["parked"] += len(row_ids)
                verb = "would park" if dry_run else "parked"
                await self._ctx.emit(
                    "error", f"reconcile {verb} batch for {product.slug}: {exc}",
                    kind="gh_reconcile_parked", target=product.project_id,
                    detail={"dry_run": dry_run, "error": str(exc)})
                continue
            if dry_run:
                continue
            payload = await self._mutate(facts.owner, doc)
            results = parse_batch(payload, alias_map)
            ok_ids: list[str] = []
            for (alias, m), rid in zip(alias_map.items(), row_ids, strict=True):
                err = results.get(alias)
                if err is None:
                    ok_ids.append(rid)
                    continue
                if await self._j_failure_count(m.key()) >= 1:
                    await self._j_park(rid, f"failed twice: {err}", now)
                    counts["parked"] += 1
                    await self._ctx.emit("error", f"reconcile parked {m.initiated_by}: {err}",
                                         kind="gh_reconcile_parked", target=product.project_id)
                else:
                    await self._j_failed(rid, err, now)
                    counts["failed"] += 1
            await self._j_applied(ok_ids, now)
            counts["applied"] += len(ok_ids)
        if not dry_run:
            await self._request_sync(product.project_id)   # re-ingest our own writes (echo-suppressed)
