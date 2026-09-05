"""PrPopulationPoller — background cache of the FULL PR population per repo.

Sibling of PrStatusPoller, but keyed by repo (not (repo, branch)): it sweeps
every open + recently-merged PR in each watched repo so the Pipeline board can
show PRs that have no ccswitch session. Stage/checks/state come from the shared
classifier (parse_population_response → _to_info); this module adds no PR logic.
"""
from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
import logging
import random
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Union

from ccswitch import gitrepos
from server.aio import cancel_and_reap
from server.github import app_auth
from .api_budget import PriorityGate
from .merge_queue import build_merge_queue_query, parse_merge_queue_response
from .pr_status import (
    PullRequestInfo,
    build_env_deployments_query,
    build_eta_checks_query,
    build_population_query,
    merge_env_deployments,
    missing_active_envs,
    parse_eta_checks_response,
    parse_merge_group_response,
    parse_population_response,
)

log = logging.getLogger(__name__)

# ETA fields carried across a failed refresh for stale-while-revalidate (#671).
# eta_stale is set separately (True on re-serve); eta_updated_at rides along so
# the UI can render "~3h (from 2h ago)".
_ETA_STALE_FIELDS = (
    "eta_seconds", "eta_low_seconds", "eta_high_seconds",
    "eta_percent", "eta_overdue", "eta_updated_at",
)


def _merged_since(retention_days: int) -> str:
    since = datetime.now(timezone.utc) - timedelta(days=retention_days)
    return since.date().isoformat()


def _default_query(repo: str, retention_days: int) -> dict:
    query = build_population_query(repo, _merged_since(retention_days))
    token = app_auth.token_for_owner(repo.split("/", 1)[0])
    proc = gitrepos._run_gh(["api", "graphql", "-f", f"query={query}"], token=token)
    try:
        response = json.loads(proc.stdout or "{}")
    except (json.JSONDecodeError, TypeError):
        response = {}
    data = response.get("data") if isinstance(response, dict) else None
    # gh exits non-zero whenever the GraphQL response carries an `errors` array —
    # even when partial `data` is usable. That happens when the App lacks
    # Deployments/Environments/Checks read: the isolated `deploys` alias (and some
    # statusCheckRollup contexts) null out, but the PR population survives. Only
    # treat a non-zero exit as fatal when there is no usable data at all; otherwise
    # degrade gracefully so the pipeline still renders PRs (issue: blank pipeline).
    usable = isinstance(data, dict) and (data.get("repo") or data.get("merged"))
    if proc.returncode != 0 and not usable:
        raise gitrepos.GhError(proc.stderr.strip() or "gh api graphql failed")
    if proc.returncode != 0:
        errs = [e.get("message") for e in (response.get("errors") or [])][:3]
        log.warning("pr_population.partial_graphql repo=%s errors=%s", repo, errs)
    _fold_deploys(response)
    _supplement_env_deployments(response, repo, token)
    return response


def _fold_deploys(response: dict) -> None:
    """Merge the isolated ``deploys`` alias back into ``data.repo`` so downstream
    parsing (``parse_population_response`` / ``_supplement_env_deployments``) reads
    deployments/environments from their historic ``data.repo`` location.

    A null ``deploys`` (permission-forbidden) leaves the repo block's PRs
    untouched. ``setdefault`` keeps this a no-op for legacy single-block responses
    that already carry deployments under ``data.repo`` (test fidelity)."""
    data = response.get("data") if isinstance(response, dict) else None
    if not isinstance(data, dict):
        return
    deploys = data.pop("deploys", None)
    repo_node = data.get("repo")
    if isinstance(repo_node, dict) and isinstance(deploys, dict):
        repo_node.setdefault("deployments", deploys.get("deployments"))
        repo_node.setdefault("environments", deploys.get("environments"))


def _supplement_env_deployments(response: dict, repo: str, token: str | None) -> None:
    """Environment-filtered re-fetch for classified envs whose newest SUCCESS
    deploy was evicted from the shared first:30 window by churn in other envs
    (a 3-deploys-per-merge repo covers only ~10 merges per window, so a
    low-churn prod env vanishes and every merged PR reverts to awaiting-prod).
    Best-effort: on failure the response keeps the plain-window behavior."""
    repo_node = ((response.get("data") or {}).get("repo")) or {}
    missing = missing_active_envs(repo_node)
    if not missing:
        return
    query = build_env_deployments_query([(repo, missing)])
    try:
        proc = gitrepos._run_gh(["api", "graphql", "-f", f"query={query}"], token=token)
        if proc.returncode != 0:
            raise gitrepos.GhError(
                proc.stderr.strip() or "gh api graphql (env deployments) failed"
            )
        env_data = (json.loads(proc.stdout or "{}").get("data")) or {}
    except Exception:
        log.warning(
            "pr_population.env_supplement_failed repo=%s envs=%s",
            repo, missing, exc_info=True,
        )
        return
    merge_env_deployments(repo_node, env_data.get("r0") or {})


def _default_eta_query(prs: list[tuple[str, int]]) -> dict:
    """Run build_eta_checks_query via gh, grouped by owner so each owner's PRs use
    that owner's App installation token. Responses are merged with aliases remapped
    to the original `prs` index, so parse_eta_checks_response(response, prs) is unchanged."""
    if not prs:
        return {}
    by_owner: dict[str, list[int]] = {}
    for i, (repo, _num) in enumerate(prs):
        by_owner.setdefault(repo.split("/", 1)[0], []).append(i)
    merged: dict[str, Any] = {}
    for owner, idxs in by_owner.items():
        subset = [prs[i] for i in idxs]
        query = build_eta_checks_query(subset)
        token = app_auth.token_for_owner(owner)
        proc = gitrepos._run_gh(["api", "graphql", "-f", f"query={query}"], token=token)
        if proc.returncode != 0:
            raise gitrepos.GhError(proc.stderr.strip() or "gh api graphql (eta) failed")
        sub_data = (json.loads(proc.stdout or "{}").get("data") or {})
        for j, global_i in enumerate(idxs):
            node = sub_data.get(f"r{j}")
            if node is not None:
                merged[f"r{global_i}"] = node
    return {"data": merged}


def _default_merge_queue_query(repos: list[str]) -> dict:
    """Run build_merge_queue_query via gh, grouped by owner so each owner's repos use
    that owner's App installation token. Responses are merged with aliases remapped to
    the original `repos` index, so parse_merge_queue_response(response, repos) is unchanged.
    This is the multi-owner generalisation of the previous first-owner-only shortcut."""
    if not repos:
        return {}
    by_owner: dict[str, list[int]] = {}
    for i, repo in enumerate(repos):
        by_owner.setdefault(repo.split("/", 1)[0], []).append(i)
    merged: dict[str, Any] = {}
    for owner, idxs in by_owner.items():
        subset = [repos[i] for i in idxs]
        query = build_merge_queue_query(subset)
        if not query:
            continue
        token = app_auth.token_for_owner(owner)
        proc = gitrepos._run_gh(["api", "graphql", "-f", f"query={query}"], token=token)
        if proc.returncode != 0:
            raise gitrepos.GhError(proc.stderr.strip() or "gh api graphql (merge queue) failed")
        sub_data = (json.loads(proc.stdout or "{}").get("data") or {})
        for j, global_i in enumerate(idxs):
            node = sub_data.get(f"r{j}")
            if node is not None:
                merged[f"r{global_i}"] = node
    return {"data": merged}


def _enrich_sync(
    prs_by_repo: dict[str, tuple[PullRequestInfo, ...]],
    checks_by_pr: dict[tuple[str, int], list],
    merge_by_pr: dict[tuple[str, int], dict],
    store,
    compute_pr_eta,
    build_check_groups,
    now: datetime,
    now_iso: str,
    eta_cache: dict[tuple[str, int], dict],
) -> dict[str, tuple[PullRequestInfo, ...]]:
    """The blocking half of ETA enrichment: SQLite reads + pure compute.

    Runs on a worker thread (RC1) — it must not touch the event loop, awaitables,
    or any asyncio primitive. ``eta_cache`` is mutated in place (same dict the
    caller holds as ``self._eta_cache``), so cache behaviour is unchanged.
    """
    result: dict[str, tuple[PullRequestInfo, ...]] = {}
    for repo, prs in prs_by_repo.items():
        enriched = []
        for pr in prs:
            if pr.state in ("open", "draft"):
                key = (repo, pr.number)
                checks = checks_by_pr.get(key, [])
                try:
                    # check_groups always (rows/durations/cost need no history);
                    # ETA/overdue only when a history store is present.
                    groups = build_check_groups(
                        checks, merge_by_pr.get(key, {}), store, repo, now,
                    )
                    fields: dict = {"check_groups": groups}
                    if compute_pr_eta is not None:
                        eta_s, eta_low, eta_high, eta_pct, eta_over = compute_pr_eta(
                            store, repo, checks, now
                        )
                        fields["eta_seconds"] = eta_s
                        fields["eta_low_seconds"] = eta_low
                        fields["eta_high_seconds"] = eta_high
                        fields["eta_percent"] = eta_pct
                        fields["eta_overdue"] = eta_over
                        # Fresh value — clear the stale flag and stamp the
                        # compute time (issue #671).
                        fields["eta_stale"] = False
                        fields["eta_updated_at"] = now_iso
                        if eta_s is not None:
                            eta_cache[key] = {
                                "eta_seconds": eta_s,
                                "eta_low_seconds": eta_low,
                                "eta_high_seconds": eta_high,
                                "eta_percent": eta_pct,
                                "eta_overdue": eta_over,
                                "eta_updated_at": now_iso,
                            }
                    pr = dataclasses.replace(pr, **fields)
                except Exception:
                    log.debug(
                        "pr_population.eta_compute_failed repo=%s pr=%s",
                        repo, pr.number, exc_info=True,
                    )
            enriched.append(pr)
        result[repo] = tuple(enriched)
    return result


def _try_load_store(db_path: str | None):  # type: ignore[return]
    """Return a HistoryStore for db_path, or None if unavailable (file absent/error)."""
    if db_path is None:
        return None
    try:
        from server.pipeline.history import HistoryStore
        return HistoryStore(db_path)
    except Exception:
        return None


class PrPopulationPoller:
    def __init__(
        self,
        poll_interval: float = 75.0,
        query_fn: Callable[[str], dict] | None = None,
        *,
        retention_days: int = 7,
        jitter_frac: float = 0.1,
        rng: Callable[[], float] = random.random,
        clock: Callable[[], float] = time.monotonic,
        backoff_base: float | None = None,
        backoff_cap: float = 900.0,
        # ETA enrichment — injectable for tests; None = use defaults
        eta_query_fn: Callable[[list[tuple[str, int]]], dict] | None = None,
        history_store=None,          # HistoryStore | None (pre-built; beats db_path)
        history_db_path: str | None = None,  # fallback: open from this path lazily
        now_fn: Callable[[], datetime] | None = None,
        merge_queue_query_fn: Callable[[list[str]], dict] | None = None,
        merge_queue_sink: Callable[..., Awaitable[None]] | None = None,
        # ETA resilience (issue #671) — all injectable for tests.
        api_gate: PriorityGate | None = None,
        eta_gate_timeout: float = 30.0,
        eta_retry_min_s: float = 5.0,
        eta_retry_max_s: float = 30.0,
        sleep_fn: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._poll_interval = poll_interval
        self._retention_days = retention_days
        self._query_fn = query_fn or (lambda repo: _default_query(repo, retention_days))
        self._jitter_frac = jitter_frac
        self._rng = rng
        self._clock = clock
        self._backoff_base = backoff_base if backoff_base is not None else poll_interval
        self._backoff_cap = backoff_cap
        self._fail_count = 0
        self._cooldown_until = 0.0
        self._cache: dict[str, tuple[PullRequestInfo, ...]] = {}
        self._repos_provider: (
            Callable[[], list[str]] | Callable[[], Awaitable[list[str]]] | None
        ) = None
        self._task: asyncio.Task | None = None
        # ETA
        self._eta_query_fn = eta_query_fn or _default_eta_query
        self._history_store = history_store  # may be None
        self._history_db_path = history_db_path
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self._store_init_attempted = history_store is not None
        self._merge_queue_query_fn = merge_queue_query_fn
        self._merge_queue_sink = merge_queue_sink
        # ETA resilience (issue #671): a shared API-budget gate the ETA fetch
        # yields to (best-effort — bounded wait), a single jittered retry on
        # failure, and a per-PR last-good ETA cache for stale-while-revalidate.
        self._api_gate = api_gate
        self._eta_gate_timeout = eta_gate_timeout
        self._eta_retry_min_s = eta_retry_min_s
        self._eta_retry_max_s = eta_retry_max_s
        self._sleep_fn = sleep_fn or asyncio.sleep
        # (repo, number) -> last successfully-computed ETA fields (incl.
        # eta_updated_at). Pruned each cycle to the current open population.
        self._eta_cache: dict[tuple[str, int], dict] = {}
        # --- PrTimeline capture state (SPEC §2) -------------------------------
        # Previous cycle's open-PR stage per (repo, number) — drives the
        # ci → ready|queue first-green transition detection.
        self._prev_stage: dict[tuple[str, int], str] = {}
        # first_green / enqueued waypoints observed live, consumed at merge.
        self._first_green: dict[tuple[str, int], str] = {}
        self._enqueued_iso: dict[tuple[str, int], str] = {}
        # (repo, number) merges already captured this process — avoids
        # re-upserting every cycle (store is first-wins, but skip is cheaper).
        self._merged_captured: set[tuple[str, int]] = set()

    def set_repos_provider(
        self, fn: Union[Callable[[], list[str]], Callable[[], Awaitable[list[str]]]]
    ) -> None:
        self._repos_provider = fn  # type: ignore[assignment]

    async def _resolve_repos(self) -> list[str]:
        if not self._repos_provider:
            return []
        result = self._repos_provider()
        if inspect.isawaitable(result):
            result = await result
        return result or []

    def cached(self, repo: str | None) -> tuple[PullRequestInfo, ...]:
        if not repo:
            return ()
        return self._cache.get(repo, ())

    def all(self) -> dict[str, tuple[PullRequestInfo, ...]]:
        return dict(self._cache)

    def _store(self):
        """Lazy-initialize the HistoryStore (once only).  Returns None if unavailable."""
        if self._store_init_attempted:
            return self._history_store
        self._store_init_attempted = True
        if self._history_db_path is not None:
            self._history_store = _try_load_store(self._history_db_path)
        else:
            # Use the operational default destination; skip silently when absent.
            try:
                from server.pipeline.history_migrate import default_dest
                self._history_store = _try_load_store(default_dest())
            except Exception:
                self._history_store = None
        return self._history_store

    def history_store(self):
        """Public accessor for the lazily-initialised HistoryStore.

        Returns the HistoryStore instance, or None when the history DB is absent
        or unavailable (e.g. the optional pipeline dependency is not installed).
        Delegates to the internal ``_store()`` method; callers outside this module
        should use this accessor instead of reaching into the protected method.
        """
        return self._store()

    async def _fetch_eta(self, open_keys: list[tuple[str, int]]) -> dict:
        """Fetch ETA checks for ``open_keys``, yielding to higher-priority
        PR-status refreshes and retrying once with jitter (issue #671).

        The ETA fetch is best-effort background work sharing one GitHub-App
        rate-limit budget with pr_poller, so before spending budget it waits
        (bounded) for the shared gate to go idle. On a transient failure it
        sleeps a 5–30 s jittered delay and retries once — most eta_fetch_failed
        events cluster in a brief rate-limit window that a single retry clears.
        The blocking gh call is offloaded to a thread each time.
        """
        async def _one() -> dict:
            if self._api_gate is not None:
                await self._api_gate.wait_until_idle(self._eta_gate_timeout)
            return await asyncio.to_thread(self._eta_query_fn, open_keys)

        try:
            return await _one()
        except Exception:
            span = max(0.0, self._eta_retry_max_s - self._eta_retry_min_s)
            delay = self._eta_retry_min_s + self._rng() * span
            log.info("pr_population.eta_fetch_retry delay=%.1fs", delay)
            await self._sleep_fn(delay)
            return await _one()

    def _apply_stale_eta(
        self, prs_by_repo: dict[str, tuple[PullRequestInfo, ...]]
    ) -> dict[str, tuple[PullRequestInfo, ...]]:
        """Re-serve each open PR's last-good ETA marked ``eta_stale=True``.

        Called when a fresh ETA fetch fails after retry: better a captioned
        estimate than a blank tile. PRs with no cached ETA (never computed) are
        returned untouched (no ETA to show).
        """
        result: dict[str, tuple[PullRequestInfo, ...]] = {}
        for repo, prs in prs_by_repo.items():
            enriched = []
            for pr in prs:
                cached = self._eta_cache.get((repo, pr.number))
                if pr.state in ("open", "draft") and cached:
                    pr = dataclasses.replace(
                        pr, eta_stale=True,
                        **{f: cached[f] for f in _ETA_STALE_FIELDS},
                    )
                enriched.append(pr)
            result[repo] = tuple(enriched)
        return result

    async def _enrich_with_eta(
        self, prs_by_repo: dict[str, tuple[PullRequestInfo, ...]]
    ) -> dict[str, tuple[PullRequestInfo, ...]]:
        """Batch-fetch ETA check timing for all open PRs and replace() the fields.

        Returns prs_by_repo unchanged (same objects) on any error so the board
        always shows data (just without ETA). The network fetch and
        ``_record_group_history`` stay on the event loop (they are genuinely
        async); the SQLite HistoryStore reads and pure compute run on a worker
        thread (RC1 — see ``_enrich_sync``).
        """
        # `store` (pipeline-history DB) may be None — ETA/overdue need it, but the
        # per-check drill-down (check_groups) does not. Do NOT bail on a missing store,
        # or the drill-down silently never appears wherever there's no history DB (prod).
        store = self._store()

        # Collect (repo, number) pairs for open PRs only.
        open_keys: list[tuple[str, int]] = []
        for repo, prs in prs_by_repo.items():
            for pr in prs:
                if pr.state in ("open", "draft"):
                    open_keys.append((repo, pr.number))

        if not open_keys:
            self._eta_cache.clear()
            return prs_by_repo

        # Drop cache entries for PRs no longer open (merged/closed/aged out) so
        # the last-good ETA store stays bounded to the live population.
        live = set(open_keys)
        self._eta_cache = {k: v for k, v in self._eta_cache.items() if k in live}

        try:
            response = await self._fetch_eta(open_keys)
            checks_by_pr = parse_eta_checks_response(response, open_keys)
            merge_by_pr = parse_merge_group_response(response, open_keys)
        except Exception:
            # Fresh fetch failed even after a jittered retry. Rather than blank
            # the board, re-serve each open PR's last-good ETA marked stale
            # (issue #671 — stale-while-revalidate).
            log.warning("pr_population.eta_fetch_failed", exc_info=True)
            return self._apply_stale_eta(prs_by_repo)

        now = self._now_fn()
        now_iso = now.isoformat()

        # Record merge-group run durations + ejects (SPEC §1 — mergeEtaSim
        # substrate). Deduped by group_sha (the batch head-commit oid): several
        # PRs in one merge train share a headCommit, so record each train once.
        # NEVER let a history-write break the poll — swallow + log.
        try:
            await self._record_group_history(merge_by_pr, now)
        except Exception:
            log.warning("pr_population.group_history_failed", exc_info=True)

        # build_check_groups is always needed (store-independent). compute_pr_eta needs
        # the history store, so import + run it only when we actually have one.
        try:
            from server.pipeline.checkgroups import build_check_groups
        except Exception:
            log.warning("pr_population.checkgroups_import_failed", exc_info=True)
            return prs_by_repo

        compute_pr_eta: Callable[..., tuple[int | None, int | None, int | None, int | None, bool]] | None = None
        if store is not None:
            try:
                from server.pipeline.eta import compute_pr_eta as _compute_pr_eta
                compute_pr_eta = _compute_pr_eta
            except Exception:
                log.warning("pr_population.eta_import_failed", exc_info=True)

        # SQLite reads + compute are blocking; run them on a worker thread so the
        # daemon's event loop keeps scheduling (RC1). HistoryStore is thread-safe
        # as of P1 Task 1.
        result = await asyncio.to_thread(
            _enrich_sync, prs_by_repo, checks_by_pr, merge_by_pr, store,
            compute_pr_eta, build_check_groups, now, now_iso, self._eta_cache,
        )
        return result

    async def _record_group_history(
        self,
        merge_by_pr: dict[tuple[str, int], dict],
        now: datetime,
    ) -> None:
        """Persist merge-group run durations + ejects (SPEC §1).

        ``merge_by_pr`` is keyed by (repo, number); each value carries the
        train's ``checks`` and ``group_sha`` (batch head-commit oid). PRs in
        the same train share a group_sha, so record each (repo, group_sha)
        once. The store applies the terminal/clean/timestamp guards and dedupes
        on its unique constraints — this method only fans out unique trains.
        """
        from server.pipeline import group_history

        now_iso = now.isoformat()
        seen: set[tuple[str, str]] = set()
        for (repo, _number), entry in merge_by_pr.items():
            group_sha = entry.get("group_sha")
            checks = entry.get("checks") or []
            if not group_sha or not checks:
                continue
            dedupe_key = (repo, group_sha)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            await group_history.record_group_outcome(
                repo, group_sha, checks, now_iso
            )

    async def _capture_timeline(
        self,
        new_cache: dict[str, tuple[PullRequestInfo, ...]],
        mq_by_repo: dict[str, tuple],
    ) -> None:
        """Capture PrTimeline waypoints from one poll cycle (SPEC §2).

        - first_green_at: an open PR's stage transitions ci → ready (or it enters
          the merge queue) for the FIRST time → remember now (Z-form).
        - enqueued_at: while a PR is observed in the queue, cache the queue
          entry's enqueued_at epoch as Z-form ISO.
        - on a newly-observed merge: upsert_merged_pr with every available
          waypoint, then evict the PR's in-memory state.

        In-memory maps are bounded: keys for PRs no longer present in the
        population (nor still merge-captured) are evicted each cycle.
        """
        from server.pipeline import timeline

        now_iso = self._now_fn().isoformat().replace("+00:00", "Z")

        # 1) Cache enqueued_at from queue observations (epoch → Z-form ISO).
        for repo, entries in mq_by_repo.items():
            for entry in entries:
                epoch = getattr(entry, "enqueued_at", None)
                num = getattr(entry, "pr_number", None)
                if epoch is None or num is None:
                    continue
                key = (repo, int(num))
                if key not in self._enqueued_iso:
                    iso = datetime.fromtimestamp(int(epoch), tz=timezone.utc).isoformat()
                    self._enqueued_iso[key] = iso.replace("+00:00", "Z")

        live_keys: set[tuple[str, int]] = set()
        for repo, prs in new_cache.items():
            for pr in prs:
                key = (repo, pr.number)
                live_keys.add(key)

                if pr.state == "merged":
                    if key in self._merged_captured:
                        continue
                    # merged_at is the required insertion trigger; skip if GitHub
                    # somehow returned a merged PR without it (defensive — the
                    # store's writer-invariant demands a value).
                    merged_at = pr.merged_at
                    if not merged_at:
                        continue
                    await timeline.upsert_merged_pr(
                        repo,
                        pr.number,
                        merged_at=merged_at,
                        title=pr.title or None,
                        url=pr.url or None,
                        merge_commit_sha=pr.merge_commit_sha or None,
                        created_at=pr.created_at or None,
                        first_green_at=self._first_green.get(key),
                        enqueued_at=self._enqueued_iso.get(key),
                    )
                    self._merged_captured.add(key)
                    # Evict consumed waypoints for this PR.
                    self._first_green.pop(key, None)
                    self._enqueued_iso.pop(key, None)
                    self._prev_stage.pop(key, None)
                    continue

                # Open/draft PR — detect the ci → ready|queue first transition.
                # "Left ci" = previously in ci, now advanced past it (ready stage
                # or entered the merge queue). First observation only.
                prev = self._prev_stage.get(key)
                # SPEC §2: green means ci → ready|queue specifically — ci → parked
                # (ci-failed / conflicting) is NOT green and must not record.
                left_ci = prev == "ci" and (pr.stage == "ready" or pr.in_merge_queue)
                if left_ci and key not in self._first_green:
                    self._first_green[key] = now_iso
                self._prev_stage[key] = pr.stage

        # 2) Bound the maps: evict keys for PRs no longer in the population and
        #    not pending a merge capture.
        self._evict_stale(live_keys)

    def _evict_stale(self, live_keys: set[tuple[str, int]]) -> None:
        for store in (self._prev_stage, self._first_green, self._enqueued_iso):
            for key in [k for k in store if k not in live_keys]:
                del store[key]
        # merged_captured retains only keys whose PR is still observed merged;
        # once a merged PR ages out of the population window it's safe to forget.
        self._merged_captured = {k for k in self._merged_captured if k in live_keys}

    async def refresh_once(self, repos: list[str]) -> bool:
        if not repos:
            return True
        any_ok = False
        any_fail = False
        new_cache: dict[str, tuple[PullRequestInfo, ...]] = {}
        for repo in repos:
            try:
                response = await asyncio.to_thread(self._query_fn, repo)
                new_cache[repo] = parse_population_response(response, repo)
                any_ok = True
            except Exception:
                log.warning("pr_population.refresh_failed repo=%s", repo, exc_info=True)
                any_fail = True
        # Enrich open PRs with ETA (best-effort; never propagates failures).
        if new_cache:
            try:
                new_cache = await self._enrich_with_eta(new_cache)
            except Exception:
                log.warning("pr_population.eta_enrich_failed", exc_info=True)
        mq_by_repo: dict[str, tuple] = {}
        if self._merge_queue_query_fn and self._merge_queue_sink:
            repos_for_mq = list(new_cache.keys()) or repos
            try:
                resp = await asyncio.to_thread(self._merge_queue_query_fn, repos_for_mq)
                by_repo = parse_merge_queue_response(resp, repos_for_mq)
                mq_by_repo = by_repo
                captured_at = int(self._now_fn().timestamp())
                await self._merge_queue_sink(by_repo, captured_at)
            except Exception:
                log.warning("pr_population.merge_queue_failed", exc_info=True)
        # Capture PrTimeline waypoints from this cycle's population + queue
        # observations (SPEC §2). Best-effort — NEVER break the poll.
        try:
            await self._capture_timeline(new_cache, mq_by_repo)
        except Exception:
            log.warning("pr_population.timeline_capture_failed", exc_info=True)
        self._cache.update(new_cache)
        if any_fail and not any_ok:
            self._fail_count += 1
            self._cooldown_until = self._clock() + min(
                self._backoff_base * (2 ** (self._fail_count - 1)), self._backoff_cap
            )
            return False
        self._fail_count = 0
        self._cooldown_until = 0.0
        return True

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="pr-population-poller")

    async def stop(self) -> None:
        if self._task is not None:
            await cancel_and_reap(self._task, phase="pr-population-poller")
            self._task = None

    async def _loop(self) -> None:
        while True:
            delay = self._poll_interval * (1 + self._jitter_frac * self._rng())
            if self._clock() < self._cooldown_until:
                delay = max(delay, self._cooldown_until - self._clock())
            await asyncio.sleep(delay)
            repos = await self._resolve_repos()
            await self.refresh_once(repos)
