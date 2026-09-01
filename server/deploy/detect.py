from __future__ import annotations
import asyncio
import json
import logging
from collections import OrderedDict
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
    # The deploy checkout's HEAD. Normally equal to live_sha, but a deploy that
    # fast-forwards the checkout without restarting the units leaves it AHEAD of the
    # running process — exposed separately so that state is visible instead of
    # masquerading as deployed (#859).
    checkout_sha: str = ""
    commits: list[dict] = field(default_factory=list)
    # What the UI's Deploy button can act on: "green" whenever SOMETHING pending is
    # deployable — the tip itself, or an older green commit (the "green frontier")
    # when the tip is red/pending. tip_ci carries the tip's own verdict.
    ci: str = "unavailable"
    tip_ci: str = "unavailable"
    # Newest pending commit whose CI is green (equals target_sha when the tip is
    # green); "" when nothing pending is deployable. deployable_count is how many of
    # `commits` (oldest-first up to that sha) a deploy of deployable_sha ships.
    deployable_sha: str = ""
    deployable_count: int = 0

    def to_dict(self) -> dict:
        return {"behind": self.behind, "liveSha": self.live_sha, "targetSha": self.target_sha,
                "checkoutSha": self.checkout_sha, "commits": self.commits, "ci": self.ci,
                "tipCi": self.tip_ci, "deployableSha": self.deployable_sha,
                "deployableCount": self.deployable_count}


# --- CI-verdict cache -------------------------------------------------------------------
# The detect loop ticks every ~30s and ci_verdict costs GitHub API round-trips (check-runs
# + branch protection). A COMPLETED verdict ("green"/"red") for a sha is treated as
# immutable, so it is cached for the life of the process (bounded LRU); "pending" and
# "unavailable" are transient and always re-queried. Note the controller's preflight gate
# calls steps.ci_verdict directly (uncached), so a red commit later re-run to green can
# still be deployed even while a stale banner verdict ages out of relevance.
_VERDICT_CACHE_MAX = 64
_verdict_cache: OrderedDict[str, tuple[str, float]] = OrderedDict()

# A "red" verdict is NOT immutable in this repo: flaky master runs are routinely
# re-run to green (the #846 teardown hang reddened several). Cache red only this
# long so a re-run's green verdict surfaces within minutes; green stays cached
# for the process lifetime (a completed green run never turns red).
_RED_TTL_S = 600.0


def _now() -> float:
    import time
    return time.monotonic()

# Cap on how many commits beyond the tip the green-frontier walk may evaluate per tick:
# a deep all-red backlog must not burn one GitHub round-trip per pending commit.
_GREEN_WALK_MAX = 10


def _cached_ci(sha: str, ci_fn: Callable[[str], str]) -> str:
    cached = _verdict_cache.get(sha)
    if cached is not None:
        verdict, stored_at = cached
        if verdict == "green" or _now() - stored_at < _RED_TTL_S:
            _verdict_cache.move_to_end(sha)
            return verdict
        del _verdict_cache[sha]   # red past its TTL: re-query (re-runs go green)
    verdict = ci_fn(sha)
    if verdict in ("green", "red"):
        _verdict_cache[sha] = (verdict, _now())
        while len(_verdict_cache) > _VERDICT_CACHE_MAX:
            _verdict_cache.popitem(last=False)
    return verdict


def edge_live_sha(health_url: str, fetch: Callable[[str], str] = _steps._default_fetch) -> str:
    """SHA the RUNNING edge reports on /healthz, or "" when no usable sha is available.

    This is the ground truth for "what is deployed" — the checkout's HEAD is only a
    proxy that lies whenever a deploy advanced the checkout but the units were never
    restarted (2026-08-30: checkout f4977c3, edge still 5516d05, banner gone, #859).
    Note the edge reports a SHORT sha (``running_git_sha``), so callers must compare
    with a prefix match, never ``==`` against a full sha. A /healthz that answers with
    schema_error (500) still carries the running sha — that is still the live code.
    """
    body = fetch(health_url)
    if not body:
        return ""
    try:
        doc = json.loads(body)
    except ValueError:
        return ""
    if not isinstance(doc, dict):
        return ""
    sha = str(doc.get("sha") or "")
    # running_git_sha() degrades to "unknown" without a .git dir — not a commit.
    return "" if sha == "unknown" else sha


def _resolve_live_sha(repo: str, checkout: str, *, git_mod,
                      live_sha_fn: Callable[[], str] | None) -> str:
    """The sha to treat as live: the edge-reported one when usable, else checkout HEAD.

    Both fallbacks log at WARNING — degrading to the checkout proxy is exactly the
    blind spot #859 is about, so it must never happen silently.
    """
    if live_sha_fn is None:
        return checkout
    live = live_sha_fn()
    if not live:
        log.warning("edge reported no usable live sha (unreachable?) — "
                    "falling back to checkout HEAD %s for %s", checkout, repo)
        return checkout
    try:
        git_mod.short_sha(repo, live)  # resolvability check only; value unused
    except Exception:
        log.warning("edge-reported live sha %r does not resolve in %s — "
                    "falling back to checkout HEAD %s", live, repo, checkout)
        return checkout
    return live


def compute_pending(repo: str, *, git_mod=_git, ci_fn: Callable[[str], str] = _steps.ci_verdict,
                    live_sha_fn: Callable[[], str] | None = None) -> PendingState:
    git_mod.fetch(repo)
    checkout = git_mod.current_sha(repo)
    target = git_mod.remote_sha(repo)
    live = _resolve_live_sha(repo, checkout, git_mod=git_mod, live_sha_fn=live_sha_fn)
    # `behind` must mean "origin/master has commits the deployment lacks", which is exactly
    # the deployable set `git log live..target`. Deriving it from raw SHA inequality instead
    # (live != target) is wrong when the deployed checkout is *ahead of* or diverged from
    # origin/master (e.g. after a squash-merge, force-push, or a rolled-back deploy): the SHAs
    # differ but the range is empty, so the banner showed "Deploy now" over "0 changes pending".
    # A single source of truth (commits) keeps `behind` and the displayed count from disagreeing.
    # Prefix match, not ==: the edge abbreviates its sha while git gives the full one (#788).
    if _steps.short_sha_match(live, target):
        return PendingState(behind=False, live_sha=live, target_sha=target,
                            checkout_sha=checkout, commits=[], ci="green", tip_ci="green")
    commits = git_mod.log_range(repo, live, target)
    if not commits:
        return PendingState(behind=False, live_sha=live, target_sha=target,
                            checkout_sha=checkout, commits=[], ci="green", tip_ci="green")
    tip_ci = _cached_ci(target, ci_fn)
    if tip_ci == "green":
        return PendingState(behind=True, live_sha=live, target_sha=target,
                            checkout_sha=checkout, commits=commits, ci="green",
                            tip_ci="green", deployable_sha=target,
                            deployable_count=len(commits))
    # The tip is red/pending: walk the pending commits newest → oldest for the newest
    # GREEN one — the "green frontier" a partial deploy can safely target — capped so a
    # deep all-red backlog does not turn one tick into N API calls. commits[0] IS the
    # tip (log_range is newest-first), already evaluated above.
    deployable_sha, deployable_count = "", 0
    for i, commit in enumerate(commits[1:_GREEN_WALK_MAX + 1], start=1):
        if _cached_ci(commit["sha"], ci_fn) == "green":
            deployable_sha = commit["sha"]
            deployable_count = len(commits) - i
            break
    return PendingState(behind=True, live_sha=live, target_sha=target,
                        checkout_sha=checkout, commits=commits,
                        ci="green" if deployable_sha else tip_ci, tip_ci=tip_ci,
                        deployable_sha=deployable_sha, deployable_count=deployable_count)


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
