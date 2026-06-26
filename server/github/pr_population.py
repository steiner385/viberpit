"""PrPopulationPoller — background cache of the FULL PR population per repo.

Sibling of PrStatusPoller, but keyed by repo (not (repo, branch)): it sweeps
every open + recently-merged PR in each watched repo so the Pipeline board can
show PRs that have no ccswitch session. Stage/checks/state come from the shared
classifier (parse_population_response → _to_info); this module adds no PR logic.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import random
import time
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Union

from ccswitch import gitrepos

from .pr_status import PullRequestInfo, build_population_query, parse_population_response

log = logging.getLogger(__name__)


def _merged_since(retention_days: int) -> str:
    since = datetime.now(timezone.utc) - timedelta(days=retention_days)
    return since.date().isoformat()


def _default_query(repo: str, retention_days: int) -> dict:
    query = build_population_query(repo, _merged_since(retention_days))
    proc = gitrepos._run_gh(["api", "graphql", "-f", f"query={query}"])
    if proc.returncode != 0:
        raise gitrepos.GhError(proc.stderr.strip() or "gh api graphql failed")
    return json.loads(proc.stdout or "{}")


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

    async def refresh_once(self, repos: list[str]) -> bool:
        if not repos:
            return True
        any_ok = False
        any_fail = False
        for repo in repos:
            try:
                response = await asyncio.to_thread(self._query_fn, repo)
                self._cache[repo] = parse_population_response(response, repo)
                any_ok = True
            except Exception:
                log.warning("pr_population.refresh_failed repo=%s", repo, exc_info=True)
                any_fail = True
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
