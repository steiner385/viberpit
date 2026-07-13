"""Deploy environment watcher (SPEC §3 — DEPLOY STATUS & CHAIN substrate).

Polls each enabled repo's configured deploy environments for their live commit
SHA (health endpoint), then, for every recently-merged PR whose merge commit is
an ancestor of that live SHA, stamps the qa/prod live waypoint (§2 timeline).

Design (see task-5 brief):

- ``http_get`` and ``gh_run`` are injectable for testability. Defaults are an
  httpx GET (5 s timeout) and ``gitrepos._run_gh`` run off-thread. Health polling
  NEVER raises — any parse/HTTP/transport error yields ``(liveSha=None,
  reachable=False)``.
- Ancestry uses the GitHub compare API (``gh api repos/{repo}/compare/{sha}...
  {liveSha}``): its ``status`` field describes head relative to base
  (base=sha, head=liveSha). ``"ahead"`` (head ahead of base) or ``"identical"``
  means sha is behind-or-equal head ⇒ sha IS an ancestor of liveSha.
  ``"behind"``/``"diverged"`` ⇒ not an ancestor.
- Ancestry re-checks are throttled per ``(sha, liveSha)`` pair (checked at most
  once ever). A new deploy produces a new liveSha ⇒ a new pair ⇒ re-check.
- ``status()`` returns the raw per-repo env cache in config order (Task 6 payload).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable

from server.db import db_session
from server.db.models import RepoDeployConfig
from server.pipeline.timeline import mark_env_live, tracked_merged

log = logging.getLogger(__name__)

# Poll cadence (Phase 4 makes it a setting). Module constant per the brief.
_DEPLOY_INTERVAL_S = 30.0

# Bound the checked-pairs set so a long-running daemon never grows it unbounded.
_MAX_CHECKED_PAIRS = 10_000

_HTTP_TIMEOUT_S = 5.0

HttpGet = Callable[[str], Awaitable[object]]
GhRun = Callable[[list[str]], object]


async def _default_http_get(url: str) -> object:
    import httpx

    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S) as client:
        return await client.get(url)


def _default_gh_run(args: list[str]) -> object:
    from ccswitch import gitrepos

    return gitrepos._run_gh(args)


class DeployWatcher:
    """Health-polls deploy envs and stamps qa/prod-live timeline waypoints."""

    def __init__(
        self,
        *,
        http_get: HttpGet | None = None,
        gh_run: GhRun | None = None,
        retention_days: int = 7,
    ) -> None:
        self._http_get = http_get or _default_http_get
        self._gh_run = gh_run or _default_gh_run
        self._retention_days = retention_days
        # {(repo, env_name): {"liveSha": str|None, "reachable": bool}}
        self._cache: dict[tuple[str, str], dict] = {}
        # Config order per repo: {repo: [env_name, ...]} — status() ordering source.
        self._env_order: dict[str, list[str]] = {}
        # Pairs (sha, liveSha) already ancestry-checked (checked at most once ever).
        self._checked: set[tuple[str, str]] = set()
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------ #
    # Health polling                                                      #
    # ------------------------------------------------------------------ #

    async def _fetch_live_sha(self, url: str, sha_key: str) -> tuple[str | None, bool]:
        """GET ``url`` → extract flat ``sha_key`` from JSON. NEVER raises.

        Returns ``(liveSha, reachable)``. Any HTTP error status, transport
        exception, or JSON parse failure → ``(None, False)``. A reachable 2xx
        whose body lacks the key or holds a non-string → ``(None, True)``.
        """
        try:
            resp = await self._http_get(url)
        except Exception:
            return (None, False)
        status = getattr(resp, "status_code", None)
        if status is None or not (200 <= int(status) < 300):
            return (None, False)
        try:
            body = json.loads(getattr(resp, "text", ""))
        except (ValueError, TypeError):
            return (None, False)
        value = body.get(sha_key) if isinstance(body, dict) else None
        if not isinstance(value, str):
            return (None, True)
        return (value, True)

    async def poll_once(self, now: datetime | None = None) -> None:
        """One health + ancestry sweep across all enabled deploy configs.

        Never raises: health failures degrade to unreachable; ancestry failures
        (gh non-zero / bad JSON) count as "not an ancestor".
        """
        now = now or datetime.now(timezone.utc)
        configs = await self._enabled_configs()
        # Reset ordering/cache for repos we're about to (re)poll so removed envs
        # don't linger. Only repos present in the enabled set appear in status().
        self._env_order = {}
        new_cache: dict[tuple[str, str], dict] = {}
        for repo, envs in configs.items():
            order: list[str] = []
            for env in envs:
                name = env.get("name")
                url = env.get("healthUrl")
                if not isinstance(name, str) or not isinstance(url, str):
                    continue
                order.append(name)
                sha_key = env.get("shaKey") or "commitSha"
                live_sha, reachable = await self._fetch_live_sha(url, sha_key)
                new_cache[(repo, name)] = {"liveSha": live_sha, "reachable": reachable}
            self._env_order[repo] = order
        self._cache = new_cache

        # Ancestry pass: stamp qa/prod-live waypoints.
        for repo in self._env_order:
            await self._reconcile_repo(repo, now)

    async def _reconcile_repo(self, repo: str, now: datetime) -> None:
        merged = await tracked_merged(repo, self._retention_days, now=now)
        candidates = [m for m in merged if m.merge_commit_sha]
        if not candidates:
            return
        now_iso = now.isoformat().replace("+00:00", "Z")
        for env_name in self._env_order.get(repo, []):
            entry = self._cache.get((repo, env_name))
            live_sha = entry.get("liveSha") if entry else None
            if not live_sha:
                continue
            is_prod = env_name in ("prod", "production")
            for pr in candidates:
                sha = pr.merge_commit_sha
                if not sha:
                    continue
                col = pr.prod_live_at if is_prod else pr.qa_live_at
                if col is not None:
                    continue  # already stamped for this env class
                if await self._is_ancestor(repo, sha, live_sha):
                    await mark_env_live(repo, pr.number, env_name, now_iso)

    # ------------------------------------------------------------------ #
    # Ancestry                                                            #
    # ------------------------------------------------------------------ #

    async def _is_ancestor(self, repo: str, sha: str, live_sha: str) -> bool:
        """True iff ``sha`` is an ancestor of ``live_sha`` per GitHub compare.

        Throttled: each ``(sha, live_sha)`` pair is resolved at most once ever.
        A yes-answer is authoritative and re-stamping is idempotent, so caching
        the pair after any answer is safe — a new deploy yields a new live_sha
        (new pair) and re-checks.
        """
        pair = (sha, live_sha)
        if pair in self._checked:
            return False
        if len(self._checked) > _MAX_CHECKED_PAIRS:
            self._checked.clear()
        self._checked.add(pair)
        status = await self._compare_status(repo, sha, live_sha)
        return status in ("ahead", "identical")

    async def _compare_status(self, repo: str, base: str, head: str) -> str | None:
        """Return the compare API ``status`` (base...head), or None on failure."""
        args = ["api", f"repos/{repo}/compare/{base}...{head}"]
        try:
            proc = await asyncio.to_thread(self._gh_run, args)
        except Exception:
            return None
        if getattr(proc, "returncode", 1) != 0:
            return None
        try:
            body = json.loads(getattr(proc, "stdout", "") or "")
        except (ValueError, TypeError):
            return None
        status = body.get("status") if isinstance(body, dict) else None
        return status if isinstance(status, str) else None

    # ------------------------------------------------------------------ #
    # Config / status                                                     #
    # ------------------------------------------------------------------ #

    async def _enabled_configs(self) -> dict[str, list[dict]]:
        from sqlalchemy import select

        async with db_session() as db:
            rows = (await db.execute(
                select(RepoDeployConfig).where(RepoDeployConfig.enabled.is_(True))
            )).scalars().all()
        out: dict[str, list[dict]] = {}
        for row in rows:
            try:
                envs = json.loads(row.environments or "[]")
            except (ValueError, TypeError):
                envs = []
            if isinstance(envs, list):
                out[row.repo] = [e for e in envs if isinstance(e, dict)]
        return out

    def status(self) -> dict[str, list[dict]]:
        """{repo: [{"name", "liveSha", "reachable"}, ...]} in config order."""
        out: dict[str, list[dict]] = {}
        for repo, order in self._env_order.items():
            envs = []
            for name in order:
                entry = self._cache.get((repo, name), {"liveSha": None, "reachable": False})
                envs.append({
                    "name": name,
                    "liveSha": entry.get("liveSha"),
                    "reachable": entry.get("reachable", False),
                })
            out[repo] = envs
        return out

    # ------------------------------------------------------------------ #
    # Lifecycle (mirrors PrPopulationPoller start/stop)                   #
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self.run(), name="deploy-watcher")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def run(self) -> None:
        while True:
            try:
                await self.poll_once()
            except Exception:  # defense in depth; poll_once already swallows
                log.exception("deploy watcher poll failed")
            await asyncio.sleep(_DEPLOY_INTERVAL_S)
