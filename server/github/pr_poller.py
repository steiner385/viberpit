"""PrStatusPoller — background cache of GitHub PR status keyed by (repo, branch).

Mirrors the shape of server.usage.poller.UsagePoller: a configurable interval,
jitter to avoid lockstep bursts, adaptive backoff on failure, and an in-memory
cache the HTTP path reads. Tests inject ``query_fn`` so no real gh call is made.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import random
import time
from typing import Awaitable, Callable, Union

from ccswitch import gitrepos
from server.aio import cancel_and_reap

from .api_budget import PriorityGate
from .pr_status import (
    PullRequestInfo,
    build_env_deployments_query,
    build_pr_query,
    merge_env_deployments,
    missing_active_envs,
    parse_pr_response,
)

log = logging.getLogger(__name__)


def _default_query(pairs: list[tuple[str, str]]) -> dict:
    if not pairs:
        return {"data": {}}
    # Group pairs by owner: one GitHub App installation token cannot span owners —
    # sending one owner's token against another owner's repo 404s, masquerading as
    # "repo missing". Build one per-stdin query per owner (preserving the
    # stdin/-F query=@- path to dodge the 128 KiB argv limit), call with that
    # owner's token, then remap local r{0,1,...} aliases back to the global
    # r{i} positions so parse_pr_response sees the expected layout.
    # Two consequences: the first:N cap is per-owner; cross-owner ordering follows
    # config order rather than GitHub's cross-repo ranking.
    owner_to_items: dict[str, list[tuple[int, tuple[str, str]]]] = {}
    for i, pair in enumerate(pairs):
        owner = pair[0].partition("/")[0]
        owner_to_items.setdefault(owner, []).append((i, pair))
    merged_data: dict = {}
    for owner, items in owner_to_items.items():
        global_idxs = [idx for idx, _ in items]
        owner_pairs = [pair for _, pair in items]
        query = build_pr_query(owner_pairs)
        if not query:
            continue
        proc = gitrepos._run_gh(
            ["api", "graphql", "-F", "query=@-"],
            token=gitrepos.app_token_for_owner(owner),
            input=query,
        )
        if proc.returncode != 0:
            log.warning(
                "pr_poller.query_failed owner=%s err=%s",
                owner, proc.stderr.strip() or "gh api graphql failed",
            )
            continue
        owner_data = (json.loads(proc.stdout or "{}").get("data")) or {}
        for local_i, global_i in enumerate(global_idxs):
            node = owner_data.get(f"r{local_i}")
            if node is not None:
                merged_data[f"r{global_i}"] = node
    response = {"data": merged_data}
    _supplement_env_deployments(response, pairs)
    return response


def _supplement_env_deployments(response: dict, pairs: list[tuple[str, str]]) -> None:
    """Environment-filtered re-fetch for classified envs whose newest SUCCESS
    deploy was evicted from the shared first:30 deployments window by churn in
    other envs (same truncation the population poller supplements). A repo
    appears once per tracked branch; the supplement is fetched once per repo
    and merged into every alias node. Best-effort: on failure the response
    keeps the plain-window behavior."""
    data = response.get("data") or {}
    alias_idxs_by_repo: dict[str, list[int]] = {}
    for i, (repo, _branch) in enumerate(pairs):
        alias_idxs_by_repo.setdefault(repo, []).append(i)
    targets = []
    for repo, idxs in alias_idxs_by_repo.items():
        missing = missing_active_envs(data.get(f"r{idxs[0]}") or {})
        if missing:
            targets.append((repo, missing))
    if not targets:
        return
    # Same per-owner split as _default_query: group targets by owner and call once
    # per owner with that owner's token so no token crosses an owner boundary.
    owner_targets: dict[str, list[tuple[str, list[str]]]] = {}
    for repo, missing in targets:
        owner = repo.partition("/")[0]
        owner_targets.setdefault(owner, []).append((repo, missing))
    for owner, o_targets in owner_targets.items():
        query = build_env_deployments_query(o_targets)
        try:
            proc = gitrepos._run_gh(
                ["api", "graphql", "-F", "query=@-"],
                token=gitrepos.app_token_for_owner(owner),
                input=query,
            )
            if proc.returncode != 0:
                raise gitrepos.GhError(
                    proc.stderr.strip() or "gh api graphql (env deployments) failed"
                )
            env_data = (json.loads(proc.stdout or "{}").get("data")) or {}
        except Exception:
            log.warning(
                "pr_poller.env_supplement_failed targets=%s",
                [t[0] for t in o_targets], exc_info=True,
            )
            continue
        for i, (repo, _missing) in enumerate(o_targets):
            env_node = env_data.get(f"r{i}") or {}
            for j in alias_idxs_by_repo[repo]:
                node = data.get(f"r{j}")
                if node:
                    merge_env_deployments(node, env_node)


class PrStatusPoller:
    def __init__(
        self,
        poll_interval: float = 75.0,
        query_fn: Callable[[list[tuple[str, str]]], dict] | None = None,
        *,
        jitter_frac: float = 0.1,
        rng: Callable[[], float] = random.random,
        clock: Callable[[], float] = time.monotonic,
        backoff_base: float | None = None,
        backoff_cap: float = 900.0,
        chunk_size: int = 40,
        api_gate: PriorityGate | None = None,
    ) -> None:
        self._poll_interval = poll_interval
        self._query_fn = query_fn if query_fn is not None else _default_query
        self._chunk_size = max(1, chunk_size)
        # Shared API-budget gate (issue #671). PR-status refresh is the HIGH
        # priority tier; when set, its network calls mark the gate busy so the
        # best-effort ETA enrichment yields. None = no gating (unit tests).
        self._api_gate = api_gate
        self._jitter_frac = jitter_frac
        self._rng = rng
        self._clock = clock
        self._backoff_base = backoff_base if backoff_base is not None else poll_interval
        self._backoff_cap = backoff_cap
        self._fail_count = 0
        self._cooldown_until = 0.0
        self._cache: dict[tuple[str, str], PullRequestInfo] = {}
        self._pairs_provider: (
            Callable[[], list[tuple[str, str]]]
            | Callable[[], Awaitable[list[tuple[str, str]]]]
            | None
        ) = None
        self._on_change: Callable | None = None
        self._on_cycle: Callable | None = None
        self._task: asyncio.Task | None = None

    def set_pairs_provider(
        self,
        fn: Union[
            Callable[[], list[tuple[str, str]]],
            Callable[[], Awaitable[list[tuple[str, str]]]],
        ],
    ) -> None:
        self._pairs_provider = fn  # type: ignore[assignment]

    async def _resolve_pairs(self) -> list[tuple[str, str]]:
        if not self._pairs_provider:
            return []
        result = self._pairs_provider()
        if inspect.isawaitable(result):
            result = await result
        return result or []

    def set_on_change(self, fn: Callable) -> None:
        """Register a callback invoked when a tracked PR's checks field changes.

        ``fn(repo: str, branch: str, old_checks: str | None, new_checks: str)``
        may be a plain function or a coroutine function — both are supported.
        A callback that raises is swallowed so it never breaks the poll sweep.
        """
        self._on_change = fn

    def set_on_cycle(self, fn: Callable) -> None:
        """Register a callback invoked once per poll cycle, AFTER ``refresh_once``.

        ``fn()`` takes no args and reads the (now-fresh) cache itself — this is the
        per-cycle SWEEP hook PR Doctor uses to evaluate every tracked PR (including
        non-CI failure modes like BEHIND/DIRTY that never fire a checks transition,
        so ``on_change`` never sees them). May be a plain function or a coroutine
        function. A callback that raises is swallowed so it never breaks the loop.
        """
        self._on_cycle = fn

    @property
    def in_cooldown(self) -> bool:
        """True while refresh failures have this poller backing off — on-demand
        callers (autopilot verify-before-directing) must not pile on."""
        return self._clock() < self._cooldown_until

    def cached(self, repo: str | None, branch: str | None) -> PullRequestInfo | None:
        if not repo or not branch:
            return None
        return self._cache.get((repo, branch))

    async def _run_query(self, chunk: list[tuple[str, str]]) -> dict:
        """Run one query chunk, marking the shared API gate busy for its
        duration so best-effort ETA fetches yield to this refresh (issue #671).
        The blocking gh call is offloaded to a thread either way."""
        if self._api_gate is None:
            return await asyncio.to_thread(self._query_fn, chunk)
        async with self._api_gate.high_priority():
            return await asyncio.to_thread(self._query_fn, chunk)

    async def refresh_once(self, pairs: list[tuple[str, str]]) -> bool:
        if not pairs:
            return True
        # GitHub's GraphQL endpoint enforces a ~10s execution budget; one
        # document with hundreds of aliased repository() blocks 502/504s.
        # Query in bounded chunks and apply each chunk's results as it lands,
        # so a failing chunk can never starve the whole cache — the
        # _pr_pairs self-prune needs cache entries to shrink the pair list,
        # and an all-or-nothing refresh deadlocked once the list outgrew the
        # execution budget.
        fresh: dict[tuple[str, str], PullRequestInfo] = {}
        ok_pairs: list[tuple[str, str]] = []
        failed = False
        for start in range(0, len(pairs), self._chunk_size):
            chunk = pairs[start:start + self._chunk_size]
            try:
                response = await self._run_query(chunk)
                fresh.update(parse_pr_response(response, chunk))
                ok_pairs.extend(chunk)
            except Exception:
                log.warning("pr_poller.refresh_failed", exc_info=True)
                failed = True
        if failed:
            self._fail_count += 1
            self._cooldown_until = self._clock() + min(
                self._backoff_base * (2 ** (self._fail_count - 1)), self._backoff_cap
            )
            if not ok_pairs:
                return False
        else:
            self._fail_count = 0
            self._cooldown_until = 0.0
        # Snapshot old values before mutating the cache so we can diff after.
        old_snapshot: dict[tuple[str, str], PullRequestInfo | None] = {
            k: self._cache.get(k) for k in ok_pairs
        }
        # Rebuild cache entries for the pairs we just queried: drop stale keys
        # (PR closed/branch gone) and refresh present ones.
        for key in ok_pairs:
            self._cache.pop(key, None)
        self._cache.update(fresh)
        # Fire the on_change callback for each pair whose checks value changed.
        if self._on_change is not None:
            for (repo, branch), new_info in fresh.items():
                old_info = old_snapshot.get((repo, branch))
                old_checks = old_info.checks if old_info is not None else None
                if old_checks != new_info.checks:
                    try:
                        result = self._on_change(repo, branch, old_checks, new_info.checks)
                        if inspect.isawaitable(result):
                            await result
                    except Exception:
                        log.warning("pr_poller.on_change callback raised", exc_info=True)
        return not failed

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="pr-status-poller")

    async def stop(self) -> None:
        if self._task is not None:
            await cancel_and_reap(self._task, phase="pr-status-poller")
            self._task = None

    async def _fire_on_cycle(self) -> None:
        """Invoke the per-cycle sweep hook defensively (mirrors ``on_change``).

        Awaits an awaitable result; wraps everything in try/except so a sweep
        error never aborts the poll loop.
        """
        if self._on_cycle is None:
            return
        try:
            result = self._on_cycle()
            if inspect.isawaitable(result):
                await result
        except Exception:
            log.warning("pr_poller.on_cycle callback raised", exc_info=True)

    async def _loop(self) -> None:
        while True:
            delay = self._poll_interval * (1 + self._jitter_frac * self._rng())
            if self._clock() < self._cooldown_until:
                delay = max(delay, self._cooldown_until - self._clock())
            await asyncio.sleep(delay)
            pairs = await self._resolve_pairs()
            await self.refresh_once(pairs)
            # Post-cycle SWEEP hook: cache is now fresh. Fires regardless of any
            # checks-transition so PR Doctor can catch non-CI failure modes.
            await self._fire_on_cycle()
