from __future__ import annotations
import asyncio
from dataclasses import dataclass, field
from typing import Callable

from server.deploy import git as _git
from server.deploy import steps as _steps


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
                 compute: Callable[[str], PendingState] = compute_pending):
        self.repo = repo
        self.interval_s = interval_s
        self._compute = compute
        self.state = PendingState()

    async def tick(self) -> None:
        try:
            self.state = await asyncio.to_thread(self._compute, self.repo)
        except Exception:
            pass   # transient git/network error — keep the last good state, retry next tick

    async def run(self, sleep=asyncio.sleep) -> None:
        while True:
            await self.tick()
            await sleep(self.interval_s)
