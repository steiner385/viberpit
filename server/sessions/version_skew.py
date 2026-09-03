"""Is a runner on the fleet's deployed version? (spec §4)

Pure predicate over what the probe already has: the daemon's two reported shas and the
``deploy_state`` row. Two checks:

* the CHECKOUT must be the deployed commit (prefix match — hosts abbreviate the same
  commit to different lengths, ``steps.short_sha_match``);
* the PROCESS must be at or after the last daemon-changing deploy (``daemon_sha``).
  "After" needs ancestry, which only the edge's checkout can answer; the probe passes
  ``is_ancestor`` in and the default memoises one CLEAN git call per (daemon_sha, process)
  pair so the 30 s probe pays it once per runner generation; a git call that RAISED is
  never cached, so a transient failure is retried on the next tick.

A runner on a commit the edge has never fetched (a hotfix branch) is not an ancestor
of anything → skewed → fenced, which is the correct answer for "not the deployed code".
"""
from __future__ import annotations

import logging
from typing import Callable

from server.deploy import git as deploy_git
from server.deploy.state import DeployState
from server.deploy.steps import short_sha_match
from server.version import repo_root

log = logging.getLogger(__name__)

IsAncestor = Callable[[str, str], bool]


# Memoised SUCCESSES only. An lru_cache would also pin FAILURES, and the common failure is
# transient and self-healing: the edge has not fetched the runner's commit yet, so the first
# probe after a deploy answers False and — cached — would keep answering False forever,
# fencing a perfectly current runner until the edge process restarts. A clean git call is a
# stable fact about immutable commits, so caching only those is safe; anything else is
# re-asked on the next 30 s probe.
_ANCESTRY_CACHE: dict[tuple[str, str], bool] = {}


def _reset_ancestry_cache_for_tests() -> None:
    _ANCESTRY_CACHE.clear()


def edge_is_ancestor(ancestor: str, descendant: str) -> bool:
    key = (ancestor, descendant)
    hit = _ANCESTRY_CACHE.get(key)
    if hit is not None:
        return hit
    try:
        answer = deploy_git.is_ancestor(repo_root(), ancestor, descendant)
    except Exception:
        log.debug("version_skew: is_ancestor(%s, %s) failed", ancestor, descendant, exc_info=True)
        return False
    _ANCESTRY_CACHE[key] = answer
    return answer


def detect_skew(*, checkout_sha: str | None, process_sha: str | None,
                state: DeployState | None, is_ancestor: IsAncestor) -> str | None:
    """``None`` when the runner is current; otherwise the ``last_error`` text."""
    if state is None:
        return None
    checkout = checkout_sha or process_sha
    # "version skew" is host_probe.FENCED_REACHABLE_PREFIXES[0] — a runner fenced by this
    # gate is still a deploy TARGET (runners.is_deployable), so the prefix is load-bearing.
    if not checkout or not short_sha_match(str(checkout), state.deployed_short):
        return (f"version skew: checkout={checkout or '?'} process={process_sha or '?'} "
                f"deployed={state.deployed_short}")
    if state.daemon_sha and process_sha:
        # short_sha_match is already prefix-symmetric in both directions — hand it the two
        # shas whole rather than pre-truncating daemon_sha to the process sha's length.
        current = (short_sha_match(str(process_sha), state.daemon_sha)
                   or is_ancestor(state.daemon_sha, str(process_sha)))
        if not current:
            return (f"version skew: checkout={checkout} process={process_sha} "
                    f"deployed={state.deployed_short} (process predates daemon_sha "
                    f"{state.daemon_sha[:7]})")
    return None
