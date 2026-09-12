"""Spec 003 §6 "rocky rule": a daemon whose code is not origin/master must not write to GitHub.

The reconciler is the only fleet-global loop that mutates state OUTSIDE ViberPit's own
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

"Is the running code the tip" has one deliberate widening (#1526): the deploy controller does
not restart the daemon for edge-only commits (docs, tests, workflows — ``steps._EDGE_ONLY``),
so master may run ahead of the daemon by exactly those commits while the daemon's code is the
tip's daemon code. When the tip is not the running sha, the fence asks GitHub's compare
endpoint (``gh api``, another ref-free network read) and opens only if every changed file is
edge-only by the SAME classifier the deploy uses; anything short of that clean answer — local
diverged, a truncated file list, any error — stays closed.
"""
from __future__ import annotations

import asyncio
import json
import subprocess

from server.github.remote_url import slug_from_remote_url
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
        usable = bool(local) and local != "unknown" and len(local) >= _MIN_SHA_LEN
        ok = usable and (remote.startswith(local)
                         or await asyncio.to_thread(_only_edge_only_ahead, local, remote))
    except Exception:
        ok = False
    _cache["fence"] = (time.monotonic(), ok)
    return ok


# GitHub's compare endpoint returns at most this many files; a full page may be
# hiding a daemon change past the cap, so a payload that reaches it is "unknown".
_COMPARE_FILE_CAP = 300
_GH_TIMEOUT_S = 15.0


def _origin_repo() -> str:
    """``owner/name`` of this checkout's ``origin`` remote (ssh or https form)."""
    proc = _run(["git", "config", "--get", "remote.origin.url"], cwd=repo_root(),
                capture_output=True, text=True, timeout=_GH_TIMEOUT_S)
    url = (getattr(proc, "stdout", "") or "").strip()
    if getattr(proc, "returncode", 1) != 0 or not url:
        raise RuntimeError("no origin remote url")
    try:
        return slug_from_remote_url(url)
    except ValueError as e:
        raise RuntimeError(f"unrecognised origin url: {url!r}") from e


def _compare(local: str, remote: str) -> dict:
    """GitHub's compare of ``local...remote`` via ``gh api`` — a network read that
    mutates no refs, like ``_remote()``. Raises on any failure or non-JSON body."""
    proc = _run(["gh", "api", f"repos/{_origin_repo()}/compare/{local}...{remote}"],
                cwd=repo_root(), capture_output=True, text=True, timeout=_GH_TIMEOUT_S)
    if getattr(proc, "returncode", 1) != 0:
        raise RuntimeError(
            f"gh compare failed: {(getattr(proc, 'stderr', '') or '').strip()[:200]}")
    payload = json.loads(getattr(proc, "stdout", "") or "")
    if not isinstance(payload, dict):
        raise RuntimeError("gh compare returned no object")
    return payload


def _only_edge_only_ahead(local: str, remote: str) -> bool:
    """True iff master is ahead of the running code ONLY by edge-only commits.

    The deploy controller restarts just the edge for docs/tests/workflow changes
    (``steps.classify_change``), so after such a merge the daemon legitimately
    runs a commit behind master while its code is byte-for-byte the tip's daemon
    code. Refusing to write then (#1526) blocked the boards for no reason. The
    same classifier decides both questions, so "does this commit need a daemon
    restart" and "is this daemon stale" cannot drift apart. Anything short of a
    clean answer — local diverged or ahead, a truncated file list, no files for
    a non-empty range, any error — reads as stale (fails closed).
    """
    from server.deploy.steps import classify_change
    cmp = _compare(local, remote)
    if cmp.get("status") not in ("identical", "ahead") or int(cmp.get("behind_by") or 0) != 0:
        return False
    raw = [f.get("filename") for f in (cmp.get("files") or []) if isinstance(f, dict)]
    files = [f for f in raw if isinstance(f, str) and f]
    if not files or len(files) != len(raw) or len(files) >= _COMPARE_FILE_CAP:
        return False
    return classify_change(files) == "edge_only"
