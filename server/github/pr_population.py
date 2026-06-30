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
from typing import Awaitable, Callable, Union

from ccswitch import gitrepos

from server.github import app_auth
from .pr_status import (
    PullRequestInfo,
    build_eta_checks_query,
    build_population_query,
    parse_eta_checks_response,
    parse_merge_group_response,
    parse_population_response,
)

log = logging.getLogger(__name__)


def _merged_since(retention_days: int) -> str:
    since = datetime.now(timezone.utc) - timedelta(days=retention_days)
    return since.date().isoformat()


def _default_query(repo: str, retention_days: int) -> dict:
    query = build_population_query(repo, _merged_since(retention_days))
    token = app_auth.token_for_owner(repo.split("/", 1)[0])
    proc = gitrepos._run_gh(["api", "graphql", "-f", f"query={query}"], token=token)
    if proc.returncode != 0:
        raise gitrepos.GhError(proc.stderr.strip() or "gh api graphql failed")
    return json.loads(proc.stdout or "{}")


def _default_eta_query(prs: list[tuple[str, int]]) -> dict:
    """Run build_eta_checks_query via gh, grouped by owner so each owner's PRs use
    that owner's App installation token. Responses are merged with aliases remapped
    to the original `prs` index, so parse_eta_checks_response(response, prs) is unchanged."""
    if not prs:
        return {}
    by_owner: dict[str, list[int]] = {}
    for i, (repo, _num) in enumerate(prs):
        by_owner.setdefault(repo.split("/", 1)[0], []).append(i)
    merged: dict = {}
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

    async def _enrich_with_eta(
        self, prs_by_repo: dict[str, tuple[PullRequestInfo, ...]]
    ) -> dict[str, tuple[PullRequestInfo, ...]]:
        """Batch-fetch ETA check timing for all open PRs and replace() the fields.

        Returns prs_by_repo unchanged (same objects) on any error so the board
        always shows data (just without ETA).  The SQLite HistoryStore is used
        on the event-loop thread; only the network call is offloaded to a thread.
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
            return prs_by_repo

        try:
            # eta_query_fn is a blocking network call — run it in a thread.
            response = await asyncio.to_thread(self._eta_query_fn, open_keys)
            checks_by_pr = parse_eta_checks_response(response, open_keys)
            merge_by_pr = parse_merge_group_response(response, open_keys)
        except Exception:
            log.warning("pr_population.eta_fetch_failed", exc_info=True)
            return prs_by_repo

        now = self._now_fn()

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

        # SQLite reads + compute run on the event-loop thread (store is not thread-safe).
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
                        pr = dataclasses.replace(pr, **fields)
                    except Exception:
                        log.debug(
                            "pr_population.eta_compute_failed repo=%s pr=%s",
                            repo, pr.number, exc_info=True,
                        )
                enriched.append(pr)
            result[repo] = tuple(enriched)
        return result

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
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            delay = self._poll_interval * (1 + self._jitter_frac * self._rng())
            if self._clock() < self._cooldown_until:
                delay = max(delay, self._cooldown_until - self._clock())
            await asyncio.sleep(delay)
            repos = await self._resolve_repos()
            await self.refresh_once(repos)
