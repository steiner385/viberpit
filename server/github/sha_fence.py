"""Spec 003 §6 "rocky rule": a daemon whose code is not origin/master must not write to GitHub.

The reconciler is the only fleet-global loop that mutates state OUTSIDE ccswitch's own
database, so a daemon running stale code could converge boards toward a superseded desired
state. This fence answers "is the running code the tip of origin/master?" and fails CLOSED —
any error (detached checkout, no git, network) reads as "not the tip", which refuses the write.
"""
from __future__ import annotations

import asyncio
import time

from server.deploy.git import remote_sha
from server.version import repo_root, running_git_sha

_cache: dict[str, tuple[float, bool]] = {}


def _remote() -> str:
    return remote_sha(repo_root(), "origin/master")


async def daemon_matches_origin_master(*, ttl_s: float = 60.0) -> bool:
    """True iff ``running_git_sha()`` is a prefix of ``origin/master``'s sha.

    ``running_git_sha()`` is short (or an injected ``CCSWITCH_GIT_SHA``), so the comparison is
    a prefix test against the full remote sha. Cached for *ttl_s* seconds: the fence runs once
    per reconcile cycle and shelling out to git on every cycle is pure overhead.
    """
    hit = _cache.get("fence")
    if hit and time.monotonic() - hit[0] < ttl_s:
        return hit[1]
    try:
        local = running_git_sha()
        remote = await asyncio.to_thread(_remote)
        ok = bool(local) and local != "unknown" and remote.startswith(local)
    except Exception:
        ok = False
    _cache["fence"] = (time.monotonic(), ok)
    return ok
