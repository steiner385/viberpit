"""Is a runner on the fleet's deployed version? (spec §4)

Pure predicate over what the probe already has: the daemon's two reported shas and the
``deploy_state`` row. Two checks:

* the CHECKOUT must be the deployed commit (prefix match — hosts abbreviate the same
  commit to different lengths, ``steps.short_sha_match``);
* the PROCESS must be at or after the last daemon-changing deploy (``daemon_sha``).
  "After" needs ancestry, which only the edge's checkout can answer; the probe passes
  ``is_ancestor`` in and the default memoises one git call per (daemon_sha, process)
  pair so the 30 s probe pays it once per runner generation.

A runner on a commit the edge has never fetched (a hotfix branch) is not an ancestor
of anything → skewed → fenced, which is the correct answer for "not the deployed code".
"""
from __future__ import annotations

import functools
import logging
from typing import Callable

from server.deploy import git as deploy_git
from server.deploy.state import DeployState
from server.deploy.steps import short_sha_match
from server.version import repo_root

log = logging.getLogger(__name__)

IsAncestor = Callable[[str, str], bool]


@functools.lru_cache(maxsize=256)
def edge_is_ancestor(ancestor: str, descendant: str) -> bool:
    try:
        return deploy_git.is_ancestor(repo_root(), ancestor, descendant)
    except Exception:
        log.debug("version_skew: is_ancestor(%s, %s) failed", ancestor, descendant, exc_info=True)
        return False


def detect_skew(*, checkout_sha: str | None, process_sha: str | None,
                state: DeployState | None, is_ancestor: IsAncestor) -> str | None:
    """``None`` when the runner is current; otherwise the ``last_error`` text."""
    if state is None:
        return None
    checkout = checkout_sha or process_sha
    if not checkout or not short_sha_match(str(checkout), state.deployed_short):
        return (f"version skew: checkout={checkout or '?'} process={process_sha or '?'} "
                f"deployed={state.deployed_short}")
    if state.daemon_sha and process_sha:
        current = (short_sha_match(str(process_sha), state.daemon_sha[:len(str(process_sha))])
                   or is_ancestor(state.daemon_sha, str(process_sha)))
        if not current:
            return (f"version skew: checkout={checkout} process={process_sha} "
                    f"deployed={state.deployed_short} (process predates daemon_sha "
                    f"{state.daemon_sha[:7]})")
    return None
