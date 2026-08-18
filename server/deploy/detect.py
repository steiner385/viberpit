from __future__ import annotations
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Callable

from server.deploy import git as _git
from server.deploy import steps as _steps

log = logging.getLogger("server.deploy.detect")


@dataclass
class PendingState:
    behind: bool = False
    live_sha: str = ""
    target_sha: str = ""
    commits: list[dict] = field(default_factory=list)
    ci: str = "unavailable"

    def to_dict(self) -> dict:
        return {"behind": self.behind, "liveSha": self.live_sha, "targetSha": self.target_sha,
                "commits": self.commits, "ci": self.ci}


def compute_pending(repo: str, *, git_mod=_git, ci_fn: Callable[[str], str] = _steps.ci_verdict) -> PendingState:
    git_mod.fetch(repo)
    live = git_mod.current_sha(repo)
    target = git_mod.remote_sha(repo)
    # `behind` must mean "origin/master has commits the deployment lacks", which is exactly
    # the deployable set `git log live..target`. Deriving it from raw SHA inequality instead
    # (live != target) is wrong when the deployed checkout is *ahead of* or diverged from
    # origin/master (e.g. after a squash-merge, force-push, or a rolled-back deploy): the SHAs
    # differ but the range is empty, so the banner showed "Deploy now" over "0 changes pending".
    # A single source of truth (commits) keeps `behind` and the displayed count from disagreeing.
    if live == target:
        return PendingState(behind=False, live_sha=live, target_sha=target, commits=[], ci="green")
    commits = git_mod.log_range(repo, live, target)
    if not commits:
        return PendingState(behind=False, live_sha=live, target_sha=target, commits=[], ci="green")
    return PendingState(behind=True, live_sha=live, target_sha=target, commits=commits, ci=ci_fn(target))


class DetectLoop:
    # Default poll cadence for the notify-only origin/master check. Tunable via
    # CCSWITCH_DEPLOY_POLL_INTERVAL (read at the construction site in app.py). 30s
    # keeps the pending-deploy banner snappy after a merge; the per-tick cost is one
    # cheap `git fetch` + one `gh api` CI check (well under the rate limit).
    def __init__(self, repo: str, *, interval_s: float = 30.0,
                 compute: Callable[[str], PendingState] = compute_pending,
                 escalate_after_consecutive: int = 3):
        self.repo = repo
        self.interval_s = interval_s
        self._compute = compute
        # A single blip keeps the last good state and logs at WARNING; a *persistently*
        # stuck poll (this many consecutive failures) escalates to ERROR so a broken repo
        # path / auth / git config surfaces instead of silently serving stale data forever.
        self.escalate_after_consecutive = escalate_after_consecutive
        self._consecutive_failures = 0
        self.state = PendingState()

    async def tick(self) -> None:
        try:
            self.state = await asyncio.to_thread(self._compute, self.repo)
        except Exception:
            # Never propagate into the poll loop: keep serving the last good state and
            # retry next tick. But don't swallow silently — a stuck poll would otherwise
            # show "nothing to deploy" indefinitely with no operator signal.
            self._consecutive_failures += 1
            level = (logging.ERROR
                     if self._consecutive_failures >= self.escalate_after_consecutive
                     else logging.WARNING)
            log.log(level, "deploy detect tick failed for %s (%d consecutive) — "
                    "serving last good state", self.repo, self._consecutive_failures,
                    exc_info=True)
        else:
            self._consecutive_failures = 0

    async def run(self, sleep=asyncio.sleep) -> None:
        while True:
            await self.tick()
            await sleep(self.interval_s)
