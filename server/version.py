"""Side-effect-free version helper.

Lives apart from ``server.app`` so the session daemon (and anything else) can read
the running git SHA without importing the edge app. Importing ``server.app`` runs
its module-level ``app = create_app()``, which performs a synchronous, blocking
daemon probe — fatal to import from the daemon's single event-loop thread, where it
deadlocks the loop. Keep this module dependency-light: no FastAPI, no network, no
app construction.
"""

import functools
import os
import subprocess
import time
from pathlib import Path


@functools.lru_cache(maxsize=1)
def running_git_sha() -> str:
    """Short git SHA of the running code, for deploy verification via /healthz.

    Resolved once: ``CCSWITCH_GIT_SHA`` (the deployer can inject it) wins; else
    ``git rev-parse --short HEAD`` against this checkout; else ``"unknown"`` (e.g.
    a source tarball with no .git). Lets a deploy assert THIS commit is live, not
    just that some process answered.
    """
    env_sha = os.environ.get("CCSWITCH_GIT_SHA")
    if env_sha:
        return env_sha.strip()
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True, text=True, timeout=5, check=True,
        )
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


_CHECKOUT_TTL_S = 10.0
_checkout_cache: tuple[float, str] | None = None   # (read_at, sha)


def repo_root() -> str:
    """The checkout this code runs from (parent of the ``server`` package)."""
    return str(Path(__file__).resolve().parents[1])


def _reset_checkout_cache_for_tests() -> None:
    global _checkout_cache
    _checkout_cache = None


def checkout_git_sha(*, now=time.monotonic, run=subprocess.run) -> str:
    """Short sha of the checkout's CURRENT HEAD, re-read at most every 10 s.

    Unlike ``running_git_sha`` (the process's start-time HEAD, cached for life), this
    follows the working tree: after an edge-only deploy fast-forwards a runner's checkout
    without restarting its daemon, the two legitimately differ, and the fleet probe's
    skew gate needs the checkout value (spec §4). ``"unknown"`` when git is unavailable.
    """
    global _checkout_cache
    t = now()
    if _checkout_cache is not None and t - _checkout_cache[0] < _CHECKOUT_TTL_S:
        return _checkout_cache[1]
    try:
        out = run(["git", "rev-parse", "--short", "HEAD"], cwd=repo_root(),
                  capture_output=True, text=True, timeout=5, check=True)
        sha = (out.stdout or "").strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        sha = "unknown"
    _checkout_cache = (t, sha)
    return sha
