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
``_remote()`` and is caught by the caller as a closed verdict: the fence fails CLOSED, refusing
writes.

"Is the running code the tip" has one deliberate widening (#1526): the deploy controller does
not restart the daemon for edge-only commits (docs, tests, workflows, repo-root tooling config
— ``steps._EDGE_ONLY``), so master may run ahead of the daemon by exactly those commits while
the daemon's code is the tip's daemon code. When the tip is not the running sha, the fence asks
GitHub's compare endpoint (``gh api``, another ref-free network read) and opens only if every
changed file is edge-only by the SAME classifier the deploy uses; anything short of that clean
answer — local diverged, a truncated file list, any error — stays closed.

The verdict carries its reason. A closed fence is invisible from the outside (the reconciler
simply applies nothing), and when the classifier missed a path class the boards starved for
days behind "daemon SHA != origin/master" with nothing naming the path. The reason is what the
status card and the automation event show.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import time
from dataclasses import dataclass

from server.github.remote_url import slug_from_remote_url
from server.version import repo_root, running_git_sha

_LS_REMOTE_TIMEOUT_S = 15.0
_MIN_SHA_LEN = 7


@dataclass(frozen=True)
class FenceVerdict:
    """The fence's answer and, when closed, WHY. Truthy iff open, so a caller that only
    asks "may I write?" reads it like the bool it replaced."""
    open: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.open

    @classmethod
    def opened(cls) -> FenceVerdict:
        return cls(True)

    @classmethod
    def closed(cls, reason: str) -> FenceVerdict:
        return cls(False, reason)


_cache: dict[str, tuple[float, FenceVerdict]] = {}

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


async def daemon_matches_origin_master(*, ttl_s: float = 60.0) -> FenceVerdict:
    """Open iff ``running_git_sha()`` is a prefix of the remote's master sha, or master is
    ahead of it only by edge-only commits.

    ``running_git_sha()`` is short (or an injected ``VIBERPIT_GIT_SHA``), so the comparison is a
    prefix test against the full 40-char remote sha; a too-short local sha (``< 7`` chars) would
    make that prefix test meaninglessly permissive and is refused. Any error from the network
    call fails closed, with the error as the reason. Cached for *ttl_s* seconds — this is a
    real remote round-trip and the reconciler asks once per cycle.
    """
    hit = _cache.get("fence")
    if hit and time.monotonic() - hit[0] < ttl_s:
        return hit[1]
    try:
        local = running_git_sha()
        if not local or local == "unknown" or len(local) < _MIN_SHA_LEN:
            verdict = FenceVerdict.closed(
                f"running sha {local!r} is unusable (need at least {_MIN_SHA_LEN} chars)")
        else:
            remote = await asyncio.to_thread(_remote)
            verdict = (FenceVerdict.opened() if remote.startswith(local)
                       else await asyncio.to_thread(_ahead_verdict, local, remote))
    except Exception as e:
        verdict = FenceVerdict.closed(
            f"could not compare the running sha with origin/master: "
            f"{type(e).__name__}: {str(e)[:200]}")
    _cache["fence"] = (time.monotonic(), verdict)
    return verdict


# GitHub's compare endpoint returns at most this many files; a full page may be
# hiding a daemon change past the cap, so a payload that reaches it is "unknown".
_COMPARE_FILE_CAP = 300
_GH_TIMEOUT_S = 15.0
# How many offending paths a refusal reason spells out (it lands on the status card).
_REASON_PATHS = 5


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


def _ahead_verdict(local: str, remote: str) -> FenceVerdict:
    """Open iff master is ahead of the running code ONLY by edge-only commits.

    The deploy controller restarts just the edge for docs/tests/workflow/tooling changes
    (``steps.classify_change``), so after such a merge the daemon legitimately runs a
    commit behind master while its code is byte-for-byte the tip's daemon code. Refusing
    to write then (#1526) blocked the boards for no reason. The same classifier decides
    both questions, so "does this commit need a daemon restart" and "is this daemon
    stale" cannot drift apart. Anything short of a clean answer — local diverged or
    ahead, a truncated file list, no files for a non-empty range, any error — reads as
    stale (fails closed), and the verdict says which.
    """
    from server.deploy.steps import daemon_changing_paths
    cmp = _compare(local, remote)
    status, behind = cmp.get("status"), int(cmp.get("behind_by") or 0)
    ahead = int(cmp.get("total_commits") or cmp.get("ahead_by") or 0)
    span = f"{local}...{remote[:7]}"
    if status not in ("identical", "ahead") or behind != 0:
        return FenceVerdict.closed(
            f"running sha {local} is {status or 'unrelated to'} origin/master "
            f"({behind} behind, {ahead} ahead)")
    raw = [f.get("filename") for f in (cmp.get("files") or []) if isinstance(f, dict)]
    files = [f for f in raw if isinstance(f, str) and f]
    if len(files) != len(raw) or len(files) >= _COMPARE_FILE_CAP:
        return FenceVerdict.closed(
            f"compare {span} file list is truncated or unreadable ({len(raw)} files): cannot "
            f"prove the {ahead} commit(s) ahead are edge-only")
    if not files:
        return FenceVerdict.closed(
            f"compare {span} listed no files for {ahead} commit(s) ahead")
    offenders = daemon_changing_paths(files)
    if offenders:
        shown = ", ".join(offenders[:_REASON_PATHS])
        if len(offenders) > _REASON_PATHS:
            shown += f" (+{len(offenders) - _REASON_PATHS} more)"
        return FenceVerdict.closed(
            f"master is {ahead} commit(s) ahead of the daemon ({local}) via daemon-changing "
            f"paths: {shown}")
    return FenceVerdict.opened()
