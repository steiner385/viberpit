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

from server.db.models import INITIATIVE_STAGES
from server.pm.desired_state import STATUS_OPTIONS, derive
from server.pm.gh_mutations import (UnknownOption, build_batch, build_create_field,
                                    build_rate_limit_query, build_update_field_options, chunk,
                                    parse_batch, parse_rate_limit)
from server.pm.gh_projects import parse_created_field
from server.pm.journal import TRANSIENT_PREFIX
from server.pm.mirror_store import ProductSyncView
from server.pm.reconcile_diff import Mutation, compile_edges, diff_fields, membership
from server.pm.reconcile_inputs import ProductFacts

from .base import AutomationStatus, ConfigField

log = logging.getLogger(__name__)

_INTERRUPTED_AFTER = timedelta(minutes=10)
#: Dry-run journal rows are a rolling preview, not an audit record: a tick re-plans the
#: same set every two minutes, so rows are deduped within this window and pruned past it.
_DRY_RUN_TTL = timedelta(hours=24)
#: How long a SUPERSEDED applied Status/Score/Stage row is kept. The current write per
#: (item, field) is never pruned however old it is — see ``journal.own_applied_values``.
_APPLIED_HISTORY_TTL = timedelta(days=30)
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
JournalOwnValues = Callable[[], Awaitable[dict[tuple[str, str], object]]]
JournalDryRunKeys = Callable[[datetime], Awaitable[set[str]]]
JournalPruneDryRun = Callable[[datetime], Awaitable[int]]
JournalPruneApplied = Callable[[datetime], Awaitable[int]]
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
        ConfigField("max_mutations_per_cycle", "Blast cap (mutations per product per tick)", "int",
                    50,
                    "Counted per PRODUCT, not per tick: a tick converging three products may "
                    "apply up to three times this. Exceeding it halts that product's cycle "
                    "loudly; nothing is applied for it, and the other products still run. Board "
                    "field bootstrap runs first and is not capped, but only while migrate is on: "
                    "up to three bounded mutations per product (create Score, create Stage, "
                    "extend Status options) may apply before this cap is evaluated."),
        ConfigField("migrate_cap", "Blast cap while migrate is on", "int", 500,
                    "Used instead of the normal cap (and likewise per product) for the one-time "
                    "migration run."),
        ConfigField("migrate", "One-time migration", "bool", False,
                    "Raise the cap and bootstrap board fields (create Score/Stage, extend the "
                    "Status options with the spec vocabulary) — the ONLY mode that edits board "
                    "schema. With it off, a board still missing a field or Status option parks "
                    "instead of being repaired. Turn off once the status card has shown "
                    "0 planned for two consecutive ticks."),
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
                 now_utc: NowUtc,
                 journal_own_values: JournalOwnValues | None = None,
                 journal_dry_run_keys: JournalDryRunKeys | None = None,
                 journal_prune_dry_run: JournalPruneDryRun | None = None,
                 journal_prune_applied: JournalPruneApplied | None = None) -> None:
        self._ctx = ctx
        self._read_config, self._list_products, self._load_facts = read_config, list_products, load_facts
        self._mutate = mutate
        self._j_record, self._j_applied, self._j_failed, self._j_park = (
            journal_record, journal_applied, journal_failed, journal_park)
        self._j_failure_count, self._j_parked, self._j_applied_keys, self._j_recover = (
            journal_failure_count, journal_parked, journal_applied_keys, journal_recover)
        # Optional journal seams (a caller that doesn't wire them degrades to the
        # pre-existing behaviour: no own-write un-sticking, no dry-run bounds).
        self._j_own_values, self._j_dry_run_keys, self._j_prune_dry_run = (
            journal_own_values, journal_dry_run_keys, journal_prune_dry_run)
        self._j_prune_applied = journal_prune_applied
        self._request_sync, self._sha_fence, self._kill_switch = request_sync, sha_fence, kill_switch
        # save_field_cache is the seam Task 9's field bootstrap (_ensure_fields) writes through.
        self._save_field_cache, self._now = save_field_cache, now_utc
        self._fence_warned = False
        # Latch for the "board fields incomplete" park emit, keyed by project id. The loop ticks
        # every two minutes and this condition needs ONE human action to clear, so an unlatched
        # emit is an error push every cycle, forever. Value is the (migrate, gap) signature so a
        # *changed* gap still speaks — that is new information.
        self._incomplete_warned: dict[str, tuple[bool, tuple[str, ...]]] = {}
        # Same latch, same reason, for dry-run vocabulary drift: an apply parks the offending
        # journal rows so its emit is self-limiting, but a dry run parks nothing and re-plans
        # the identical drift every tick. Keyed project id → the drift already reported.
        self._dry_drift_warned: dict[str, set[str]] = {}
        # Same latch for the blast-cap halt: it applies nothing, so the identical oversized set
        # is re-planned and re-reported every tick until a human raises the cap or the
        # decisions change. Keyed project id → the (planned, cap) overshoot already reported,
        # so a *different* overshoot (one more issue arrived) still speaks.
        self._cap_warned: dict[str, tuple[int, int]] = {}

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
            # Drop the previous run's counts. Leaving them stashed makes the status card
            # keep reporting "12 applied, 3 parked" for a loop that is switched off —
            # which reads as live activity from an automation that is doing nothing.
            setattr(self._ctx, "gh_reconcile_counts", None)
            self._fence_warned = False
            self._incomplete_warned.clear()
            self._dry_drift_warned.clear()
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
        if self._j_prune_dry_run is not None:
            await self._j_prune_dry_run(now - _DRY_RUN_TTL)
        if self._j_prune_applied is not None:
            await self._j_prune_applied(now - _APPLIED_HISTORY_TTL)
        migrate = bool(config.get("migrate"))
        cap = int(config.get("migrate_cap", 500)) if migrate else \
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
        # Our own applied values, sampled once per cycle beside applied_keys: the diff
        # engine needs them to tell "a human set this" from "we set this last cycle".
        own_values = await self._j_own_values() if self._j_own_values is not None else {}
        # Dry-run dedupe set, also once per cycle — only a dry run can produce dry-run rows.
        dry_keys: set[str] = set()
        if dry_run and self._j_dry_run_keys is not None:
            dry_keys = await self._j_dry_run_keys(now - _DRY_RUN_TTL)
        for product in products:
            counts["products"] += 1
            try:
                await self._converge(product, now, dry_run, cap, applied_keys, counts,
                                     migrate=migrate, own_values=own_values, dry_keys=dry_keys)
            except Exception:
                log.warning("gh_reconcile.product_failed", exc_info=True)
                await self._ctx.emit("warn", f"reconcile failed for {product.slug}",
                                     kind="gh_reconcile_error", target=product.project_id)
        self._stash(**counts)

    async def _ensure_fields(self, facts: ProductFacts, now: datetime, dry_run: bool,
                             counts: dict[str, int]) -> dict:
        """Board field bootstrap (spec 003 §4, Task 9): create the custom ``Score``/``Stage``
        fields when missing, and extend the built-in ``Status`` field's options with the spec
        vocabulary when its cached options don't already cover it. Single mutations (not part of
        ``build_batch``'s aliasing), journaled the same write-ahead way as every other mutation.
        A dry run records what it would do and returns the cache unchanged — it neither mutates
        the board nor updates the cache, since predicting drift is the diff engine's job, not
        this bootstrap's.

        Runs ONLY while ``migrate`` is on (see ``_field_gap``): bootstrapping is a one-time
        migration step, and ``updateProjectV2Field`` REPLACES a single-select's option set, so
        an unattended steady-state run of it is a board-editing hazard. The Status update is
        therefore sent as the UNION of the board's existing options (in their order) and any
        missing spec names appended, so a human's extra COLUMN is never dropped.

        Its VALUES need one more thing. Probed live on a scratch board (#987,
        ``spike_gh_projects_v2.py optionids``): an input entry without an ``id`` is a NEW
        option to ``updateProjectV2Field`` even when the name matches, so a names-only union
        regenerates every option id and silently blanks the Status of every item parked on
        those columns. Each preserved option therefore carries its existing id straight out of
        ``field_cache`` (``option_ids`` on ``build_update_field_options``); the probe confirmed
        ids and item assignments survive that way. The runbook keeps the caveat for anyone
        hand-rolling a repair without the ids.

        A bootstrap mutation that fails twice PARKS exactly like an item mutation: a board whose
        field creation is broken must not be retried at every tick forever. The caller re-runs
        ``_field_gap`` on the returned cache for exactly that reason — this method reports what
        it achieved, not what it intended."""
        cache = dict(facts.field_cache)
        wanted: list[tuple[Mutation, str]] = []
        # "Missing" is judged on field_id, the SAME predicate as ``_field_gap`` — bootstrap
        # must be able to repair exactly what the gate refuses to write against, or a cache
        # entry with no usable id parks the product on every tick with nothing that can fix
        # it. (A create that then collides on name fails, and parks, after two attempts.)
        def _absent(name: str) -> bool:
            return not ((cache.get(name) or {}).get("field_id"))

        if _absent("Score"):
            wanted.append((Mutation("create_field", facts.project_node_id or "", "Score", None,
                                    {"dataType": "NUMBER"}, f"fields:{facts.slug}"),
                           build_create_field(facts.project_node_id or "", "Score", "NUMBER", None)))
        if _absent("Stage"):
            wanted.append((Mutation("create_field", facts.project_node_id or "", "Stage", None,
                                    {"dataType": "SINGLE_SELECT", "options": list(INITIATIVE_STAGES)},
                                    f"fields:{facts.slug}"),
                           build_create_field(facts.project_node_id or "", "Stage", "SINGLE_SELECT",
                                              list(INITIATIVE_STAGES))))
        status = cache.get("Status") or {}
        have = list(status.get("options") or {})
        if status.get("field_id") and not set(STATUS_OPTIONS) <= set(have):
            union = have + [n for n in STATUS_OPTIONS if n not in have]
            wanted.append((Mutation("set_field_options", status["field_id"], "Status",
                                    have, union, f"fields:{facts.slug}"),
                           build_update_field_options(status["field_id"], union,
                                                      option_ids=status.get("options"))))
        if not wanted:
            return cache
        batch_id = f"fields-{now.strftime('%Y%m%dT%H%M%S')}"
        for m, doc in wanted:
            if await self._j_parked(m.key()):
                continue
            (rid,) = await self._j_record([m], batch_id, now, dry_run)
            counts["planned"] += 1
            if dry_run:
                continue
            payload = await self._mutate(facts.owner, doc)
            parsed = parse_created_field(payload)
            if parsed is None:
                err = str((payload.get("errors") or [{}])[0].get("message") or "no field returned")
                if await self._j_failure_count(m.key()) >= 1:
                    await self._j_park(rid, f"failed twice: {err}", now)
                    counts["parked"] += 1
                    await self._ctx.emit(
                        "error", f"field bootstrap parked for {facts.slug}: {err}",
                        kind="gh_reconcile_parked", target=facts.project_id,
                        detail={"field": m.field, "error": err})
                else:
                    await self._j_failed(rid, err, now)
                    counts["failed"] += 1
                    await self._ctx.emit("error", f"field bootstrap failed for {facts.slug}: {err}",
                                         kind="gh_reconcile_error", target=facts.project_id)
                continue
            field_id, name, options = parsed
            cache[name or (m.field or "")] = {"field_id": field_id, "options": options}
            await self._j_applied([rid], now)
            counts["applied"] += 1
        if not dry_run and facts.gh_project_db_id:
            await self._save_field_cache(facts.gh_project_db_id, cache)
        return cache

    @staticmethod
    def _field_gap(cache: dict) -> list[str]:
        """What the board's field cache is missing for item writes to be safe.

        Empty ⇒ Score/Stage exist and Status carries every spec option, so a
        ``set_field`` mutation can be built for any decision the engine makes.

        Presence is judged on ``field_id``, not on the cache key: ``_render`` raises
        ``UnknownOption`` for a cached field whose ``field_id`` is missing or empty,
        so a key alone is not enough to make a write safe. A half-written cache entry
        (a bootstrap that created the field but whose response carried no id, or an
        older sync shape) would otherwise read as "no gap" and park every item row in
        the batch instead of the one product."""
        gap = [name for name in ("Score", "Stage")
               if not ((cache.get(name) or {}).get("field_id"))]
        status = cache.get("Status") or {}
        if not status.get("field_id"):
            gap.append("Status")
            return gap
        options = status.get("options") or {}
        missing = [o for o in STATUS_OPTIONS if o not in options]
        if missing:
            gap.append("Status options: " + ", ".join(missing))
        return gap

    async def _park_incomplete(self, product: ProductSyncView, gap: list[str],
                               counts: dict[str, int], *, migrate: bool) -> None:
        """Park a product whose board fields are incomplete: count it, emit at most once.

        The park is counted every cycle — the status card should keep showing it — but the emit
        is latched per product (see ``_incomplete_warned``). ``_run`` never resets this latch;
        only a cycle that finds the gap closed does, so the next regression is loud again."""
        counts["parked"] += 1
        signature = (migrate, tuple(gap))
        if self._incomplete_warned.get(product.project_id) == signature:
            return
        self._incomplete_warned[product.project_id] = signature
        detail = "; ".join(gap)
        message = (
            f"reconcile parked {product.slug}: board field bootstrap did not complete ({detail}); "
            "item mutations skipped — fix the failed field mutation, then re-run"
            if migrate else
            f"reconcile parked {product.slug}: board fields incomplete ({detail}); "
            "turn migrate on to bootstrap")
        await self._ctx.emit("error", message, kind="gh_reconcile_parked",
                             target=product.project_id,
                             detail={"missing": list(gap), "migrate": migrate})

    async def _converge(self, product: ProductSyncView, now: datetime, dry_run: bool, cap: int,
                        applied_keys: set[str], counts: dict[str, int], *, migrate: bool = False,
                        own_values: dict[tuple[str, str], object] | None = None,
                        dry_keys: set[str] | None = None) -> None:
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
        # Task 9 bootstraps missing fields/options into this cache once per product, before
        # anything else — a board can need its Score/Stage fields created (or its built-in
        # Status options extended to the spec vocabulary) even on a cycle with zero item-level
        # mutations, so this runs before `planned` rather than gated behind it. It is gated on
        # `migrate` instead: field *creation* is a one-time migration act (and the Status
        # option update REPLACES the option set), so steady state never edits board schema.
        if migrate:
            cache = await self._ensure_fields(facts, now, dry_run, counts)
            # Bootstrap is best-effort: a field mutation can fail, park, or be skipped because
            # it is already parked, and it returns the cache it managed to build either way.
            # Re-check the gap so a half-done bootstrap stops HERE instead of cascading — every
            # set_field for the still-missing field would raise UnknownOption in build_batch and
            # park a whole 20-row batch, burying the one real failure under twenty derived ones.
            # A dry run is exempt: it writes nothing, so its cache is unchanged by construction
            # (nothing to cascade into, and gating it would erase the preview entirely).
            gap = self._field_gap(cache)
            if gap and not dry_run:
                await self._park_incomplete(product, gap, counts, migrate=True)
                return
        else:
            cache = dict(facts.field_cache)
            gap = self._field_gap(cache)
            if gap:
                # Outside migrate we do NOT repair the board — a set_field for a missing
                # field/option cannot be written safely, so the product parks for a human.
                await self._park_incomplete(product, gap, counts, migrate=False)
                return
        self._incomplete_warned.pop(product.project_id, None)
        # 2. desired state + 3. diff
        planned: list[Mutation] = []
        for item in facts.items:
            mirror = facts.mirror.get(item.ref)
            if mirror is not None:
                planned.extend(diff_fields(derive(item), mirror, own_values or {}))
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
            # Latched like the incomplete-board park (see ``_cap_warned``): the halt applies
            # nothing, so the same overshoot is re-planned every tick until a human acts.
            overshoot = (len(planned), cap)
            if self._cap_warned.get(product.project_id) != overshoot:
                self._cap_warned[product.project_id] = overshoot
                await self._ctx.emit("error", f"reconcile halted for {product.slug}: {len(planned)} "
                                     f"mutations exceed cap {cap}; nothing applied",
                                     kind="gh_reconcile_cap", target=product.project_id,
                                     detail={"planned": len(planned), "cap": cap})
            return
        self._cap_warned.pop(product.project_id, None)     # fits again -> next overshoot is loud
        # 4. apply, write-ahead journaled per batch
        batch_id = f"{'dry' if dry_run else 'run'}-{now.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
        drift: set[str] = set()          # vocabulary drift seen this cycle; latches the dry-run emit
        for batch in chunk(planned):
            # A dry run re-plans the same set every tick; only journal the mutations that
            # don't already have a fresh dry-run row (counts["planned"] still counts all).
            record = [m for m in batch if m.key() not in dry_keys] if (
                dry_run and dry_keys) else batch
            row_ids = await self._j_record(record, batch_id, now, dry_run) if record else []
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
                counts["parked"] += len(batch)
                # Sibling of the incomplete-board latch: an APPLY run parks the journal rows, so
                # the same drift is never replanned and this emit is self-limiting. A DRY run
                # parks nothing by design, so it re-plans and re-raises identical drift every
                # two minutes — latch it per (product, error) the same way, or leaving dry run
                # on (which the runbook's review step asks for) is an error push forever.
                drift.add(str(exc))
                if dry_run and str(exc) in self._dry_drift_warned.get(product.project_id, set()):
                    continue
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
            if results.failed_transiently():
                # The DOCUMENT failed (502, abuse limit, timeout), so every alias carries
                # the same error and none of them was actually judged. Recording these as
                # ordinary failures would park all 20 rows on the second such blip — the
                # marker keeps them visible in the journal but out of `failure_count`.
                for rid in row_ids:
                    await self._j_failed(rid, TRANSIENT_PREFIX + (results.document_error or ""),
                                         now)
                counts["failed"] += len(row_ids)
                await self._ctx.emit(
                    "warn", f"reconcile batch failed for {product.slug}: "
                    f"{results.document_error}; will retry next tick",
                    kind="gh_reconcile_error", target=product.project_id,
                    detail={"transient": True, "error": results.document_error,
                            "mutations": len(row_ids)})
                continue
            ok_ids: list[str] = []
            for (alias, m), rid in zip(alias_map.items(), row_ids, strict=True):
                err = results.errors.get(alias)
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
        # Replace, don't union: drift that has been fixed drops out of the latch, so if it ever
        # comes back it is reported again.
        self._dry_drift_warned[product.project_id] = drift
        if not dry_run:
            await self._request_sync(product.project_id)   # re-ingest our own writes (echo-suppressed)
