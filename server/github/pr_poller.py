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

from .pr_status import PullRequestInfo, build_pr_query, parse_pr_response

log = logging.getLogger(__name__)


def _default_query(pairs: list[tuple[str, str]]) -> dict:
    query = build_pr_query(pairs)
    if not query:
        return {"data": {}}
    proc = gitrepos._run_gh(["api", "graphql", "-f", f"query={query}"])
    if proc.returncode != 0:
        raise gitrepos.GhError(proc.stderr.strip() or "gh api graphql failed")
    return json.loads(proc.stdout or "{}")


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
    ) -> None:
        self._poll_interval = poll_interval
        self._query_fn = query_fn if query_fn is not None else _default_query
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

    def cached(self, repo: str | None, branch: str | None) -> PullRequestInfo | None:
        if not repo or not branch:
            return None
        return self._cache.get((repo, branch))

    async def refresh_once(self, pairs: list[tuple[str, str]]) -> bool:
        if not pairs:
            return True
        try:
            response = await asyncio.to_thread(self._query_fn, pairs)
            fresh = parse_pr_response(response, pairs)
        except Exception:
            log.warning("pr_poller.refresh_failed", exc_info=True)
            self._fail_count += 1
            self._cooldown_until = self._clock() + min(
                self._backoff_base * (2 ** (self._fail_count - 1)), self._backoff_cap
            )
            return False
        self._fail_count = 0
        self._cooldown_until = 0.0
        # Snapshot old values before mutating the cache so we can diff after.
        old_snapshot: dict[tuple[str, str], PullRequestInfo | None] = {
            k: self._cache.get(k) for k in pairs
        }
        # Rebuild cache entries for the pairs we just queried: drop stale keys
        # (PR closed/branch gone) and refresh present ones.
        for key in pairs:
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
        return True

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="pr-status-poller")

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
            pairs = await self._resolve_pairs()
            await self.refresh_once(pairs)
