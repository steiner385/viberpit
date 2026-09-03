"""Spec 003 §6 "rocky rule": a daemon whose code is not origin/master must not write to GitHub.

The reconciler is the only fleet-global loop that mutates state OUTSIDE ccswitch's own
database, so a daemon running stale code could converge boards toward a superseded desired
state. This fence answers "is the running code the tip of master ON GITHUB?".

It deliberately does NOT compare against the local ``origin/master`` tracking ref: that ref is
only as fresh as the last ``git fetch``, and the exact host this rail exists for (rocky, whose
deploy is disabled) has a frozen tracking ref pointing at the very commit its stale daemon runs
— so a tracking-ref comparison would say "up to date" and open the write path. Instead it asks
the remote directly with ``git ls-remote``, which reads the true tip and mutates no refs (the
deploy controller owns this checkout's refs; fetching them here would be a side effect on
someone else's state).

Every failure mode — non-zero exit, empty output, timeout, no git, no network — raises out of
``_remote()`` and is caught by the caller as False: the fence fails CLOSED, refusing writes.
"""
from __future__ import annotations

import asyncio
import subprocess
import time

from server.version import repo_root, running_git_sha

_LS_REMOTE_TIMEOUT_S = 15.0
_MIN_SHA_LEN = 7

_cache: dict[str, tuple[float, bool]] = {}

# Subprocess seam (tests monkeypatch this).
_run = subprocess.run


def _remote() -> str:
    """The true ``refs/heads/master`` sha on the origin remote, via ``git ls-remote``.

    Network call, no ref mutation. Raises on non-zero exit, timeout, or output that
    carries no sha — the caller turns any exception into a closed fence.
    """
    proc = _run(["git", "ls-remote", "--quiet", "origin", "refs/heads/master"],
                cwd=repo_root(), capture_output=True, text=True,
                timeout=_LS_REMOTE_TIMEOUT_S)
    if getattr(proc, "returncode", 1) != 0:
        raise RuntimeError(
            f"git ls-remote failed: {(getattr(proc, 'stderr', '') or '').strip()[:200]}")
    first = (getattr(proc, "stdout", "") or "").strip().splitlines()
    sha = first[0].split()[0] if first and first[0].split() else ""
    if len(sha) != 40:
        raise RuntimeError(f"git ls-remote returned no master sha: {sha!r}")
    return sha


async def daemon_matches_origin_master(*, ttl_s: float = 60.0) -> bool:
    """True iff ``running_git_sha()`` is a prefix of the remote's master sha.

    ``running_git_sha()`` is short (or an injected ``CCSWITCH_GIT_SHA``), so the comparison is a
    prefix test against the full 40-char remote sha; a too-short local sha (``< 7`` chars) would
    make that prefix test meaninglessly permissive and is refused. Any error from the network
    call fails closed. Cached for *ttl_s* seconds — this is a real remote round-trip and the
    reconciler asks once per cycle.
    """
    hit = _cache.get("fence")
    if hit and time.monotonic() - hit[0] < ttl_s:
        return hit[1]
    try:
        local = running_git_sha()
        remote = await asyncio.to_thread(_remote)
        ok = (bool(local) and local != "unknown" and len(local) >= _MIN_SHA_LEN
              and remote.startswith(local))
    except Exception:
        ok = False
    _cache["fence"] = (time.monotonic(), ok)
    return ok
