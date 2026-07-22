"""Orphaned-worktree janitor — sweeps stale ccswitch worktrees.

Called by the orchestrator's ``_worktree_janitor_loop`` once at startup then
every 24 h.  All git calls are synchronous subprocess calls; run inside
``asyncio.to_thread`` so the event loop is never blocked.

Safety invariants
-----------------
* Never calls ``git worktree prune`` globally (issue #177 — it can reap a
  sibling session's live worktree).  Uses only scoped ``git worktree remove``
  via the existing ``remove_worktree()`` helper.
* Two independent dirty guards: ``_is_clean()`` here, then ``remove_worktree()``
  repeats the check internally.
* Any subprocess failure or dir-level exception → treat conservatively → keep.
* A repo with no remotes configured → ``_is_fully_pushed()`` returns False →
  keep (unverifiable).
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Iterator

from ccswitch.worktrees import remove_worktree

log = logging.getLogger(__name__)


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True)


def _candidate_worktree_dirs(repo_root: str) -> Iterator[Path]:
    """Yield every ``<repo_root>/.ccswitch/worktrees/<repo>/<short-id>/`` dir."""
    base = Path(repo_root) / ".ccswitch" / "worktrees"
    if not base.is_dir():
        return
    for repo_dir in base.iterdir():
        if repo_dir.is_dir():
            for wt_dir in repo_dir.iterdir():
                if wt_dir.is_dir():
                    yield wt_dir


def _is_clean(wt: str) -> bool:
    """True iff ``git status --porcelain`` returns no output."""
    r = _run(["git", "-C", wt, "status", "--porcelain"])
    return r.returncode == 0 and r.stdout.strip() == ""


def _upstream_gone(wt: str) -> bool:
    """True iff the current branch's remote-tracking ref has been deleted.

    ``git branch -vv`` marks a branch ``[gone]`` when its upstream ref was
    deleted — the squash-merge signature: branch merged, GitHub deleted the
    remote ref, but the local child branch still exists.
    """
    r = _run(["git", "-C", wt, "branch", "-vv"])
    if r.returncode != 0:
        return False
    for line in r.stdout.splitlines():
        if line.startswith("* ") and ": gone]" in line:
            return True
    return False


def _is_fully_pushed(wt: str) -> bool:
    """True iff all HEAD commits are pushed to a remote, OR upstream is gone.

    Two cases:
    1. Upstream ``[gone]``: squash-merge landed; remote branch deleted.  Work
       is safely in the remote default branch, so we can reclaim.
    2. ``rev-list HEAD --not --remotes`` is empty: every commit reachable from
       any remote ref.

    Guard: a repo with no remotes makes ``--remotes`` vacuously empty, which
    would make the rev-list vacuously empty too.  Check for remotes first.
    """
    if _upstream_gone(wt):
        return True
    if _run(["git", "-C", wt, "remote"]).stdout.strip() == "":
        return False
    r = _run(["git", "-C", wt, "rev-list", "HEAD", "--not", "--remotes"])
    if r.returncode != 0:
        return False
    return r.stdout.strip() == ""


def sweep_orphaned_worktrees(
    repo_root: str,
    live_cwds: frozenset[str],
    now: float | None = None,
    min_age: float = 86400.0,
) -> dict[str, int]:
    """Walk managed worktree dirs and remove those that are safe to reclaim.

    A dir is reclaimed only when ALL of the following hold:
    - Its absolute path is NOT in ``live_cwds`` (not a live session).
    - Its mtime is at least ``min_age`` seconds old (default 24 h).
    - ``git status`` shows a clean working tree.
    - All commits are pushed to a remote (or the remote branch is gone,
      i.e. the branch was squash-merged and deleted).

    Returns a count dict with keys:
      ``scanned``, ``removed``, ``kept_live``, ``kept_young``,
      ``kept_dirty``, ``kept_unpushed``, ``kept_error``
    """
    import time as _time

    if now is None:
        now = _time.time()

    counts: dict[str, int] = {
        "scanned": 0,
        "removed": 0,
        "kept_live": 0,
        "kept_young": 0,
        "kept_dirty": 0,
        "kept_unpushed": 0,
        "kept_error": 0,
    }

    for wt_path in _candidate_worktree_dirs(repo_root):
        counts["scanned"] += 1
        wt = str(wt_path.resolve())
        try:
            if wt in live_cwds:
                counts["kept_live"] += 1
                continue

            try:
                mtime = wt_path.stat().st_mtime
            except OSError:
                counts["kept_error"] += 1
                continue
            if now - mtime < min_age:
                counts["kept_young"] += 1
                continue

            if not _is_clean(wt):
                log.info("worktree-janitor: keep_dirty path=%s", wt)
                counts["kept_dirty"] += 1
                continue

            if not _is_fully_pushed(wt):
                log.info("worktree-janitor: keep_unpushed path=%s", wt)
                counts["kept_unpushed"] += 1
                continue

            removed = remove_worktree(wt)
            if removed:
                counts["removed"] += 1
            else:
                counts["kept_error"] += 1
        except Exception:
            log.warning("worktree-janitor: error processing path=%s", wt, exc_info=True)
            counts["kept_error"] += 1

    return counts
