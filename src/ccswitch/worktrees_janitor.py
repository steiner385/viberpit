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
* Dirty worktrees whose backing session is no longer active are stash-evicted
  after ``max_dirty_age`` seconds (default 7 days).  The stash SHA is logged
  for recovery.  If stashing fails the worktree is still force-evicted
  (``git worktree remove --force``) — 7 days without an active session means
  the changes are abandoned.
"""
from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Iterator

from ccswitch.clone_doctor import clone_dir_from_gitdir, read_gitdir_pointer
from ccswitch.worktrees import remove_worktree

log = logging.getLogger(__name__)

# A dirty worktree older than this is eligible for stash-eviction when no
# session owns it (not in live_cwds).  7 days is conservative enough that
# a legitimately long-running session won't be caught.
_MAX_DIRTY_AGE_DEFAULT: float = 7 * 86400.0

# Operator alert thresholds for accumulated dirty worktrees.
_DIRTY_WARN_THRESHOLD = 30
_DIRTY_CRITICAL_THRESHOLD = 50


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


def _orphaned_clone(wt: str) -> str | None:
    """The missing clone this worktree points at, or None if its link resolves.

    A session worktree is a pointer file into ``<clone>/.git/worktrees/<id>``;
    when the shared clone is deleted, every git command in the tree fails with
    ``fatal: not a git repository`` while the files sit there intact (#1003).
    Undetected, such a worktree reads as "dirty" (git errors → non-empty status
    check fails) and inflates the dirty backlog alert instead of naming the real
    cause. Dirs with no pointer file at all are NOT orphans — they were never
    linked worktrees.
    """
    gitdir = read_gitdir_pointer(wt)
    if not gitdir or os.path.isdir(gitdir):
        return None
    return clone_dir_from_gitdir(gitdir) or gitdir


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


def _git_stash(wt: str) -> str | None:
    """Stash all uncommitted changes (including untracked files) in *wt*.

    Returns the stash-commit SHA on success, or ``None`` if stashing failed.
    Injects a dummy git identity so the stash commit can be created even when
    the worktree has no user.name / user.email configured.

    The stash persists in the parent repo's object store after the worktree is
    removed, so the SHA can be used to recover the changes with
    ``git stash apply <sha>`` from the main repo.
    """
    r = _run([
        "git", "-C", wt,
        "-c", "user.email=ccswitch-janitor@local",
        "-c", "user.name=ccswitch-janitor",
        "stash", "push", "-u",
        "-m", "ccswitch-janitor: max-age eviction",
    ])
    if r.returncode != 0:
        return None
    sha = _run(["git", "-C", wt, "rev-parse", "refs/stash"])
    if sha.returncode != 0:
        return None
    return sha.stdout.strip()


def sweep_orphaned_worktrees(
    repo_root: str,
    live_cwds: frozenset[str],
    now: float | None = None,
    min_age: float = 86400.0,
    max_dirty_age: float = _MAX_DIRTY_AGE_DEFAULT,
) -> dict[str, int]:
    """Walk managed worktree dirs and remove those that are safe to reclaim.

    A dir is reclaimed only when ALL of the following hold:
    - Its absolute path is NOT in ``live_cwds`` (not a live session).
    - Its mtime is at least ``min_age`` seconds old (default 24 h).
    - ``git status`` shows a clean working tree.
    - All commits are pushed to a remote (or the remote branch is gone,
      i.e. the branch was squash-merged and deleted).

    **Max-age dirty eviction**: a dirty worktree that is not in ``live_cwds``
    and whose mtime exceeds ``max_dirty_age`` (default 7 days) is stashed and
    then force-removed.  The stash SHA is logged for recovery.  If stashing
    fails the worktree is still force-removed (changes are considered
    abandoned after 7 days with no active session).

    Returns a count dict with keys:
      ``scanned``, ``removed``, ``kept_live``, ``kept_young``,
      ``kept_dirty``, ``kept_unpushed``, ``kept_error``, ``evicted_dirty``
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
        "evicted_dirty": 0,
        "orphaned": 0,
    }
    orphan_clones: set[str] = set()

    for wt_path in _candidate_worktree_dirs(repo_root):
        counts["scanned"] += 1
        wt = str(wt_path.resolve())
        try:
            # Checked BEFORE the live-session skip: an orphaned worktree is
            # broken whether or not a session is sitting in it, and a live one is
            # exactly the outage we want named in the journal (#1003). Never
            # removed — the files are intact and recoverable.
            missing_clone = _orphaned_clone(wt)
            if missing_clone:
                counts["orphaned"] += 1
                orphan_clones.add(missing_clone)
                continue

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
                age = now - mtime
                if age >= max_dirty_age:
                    _evict_dirty(wt, age, counts)
                else:
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

    _emit_dirty_alerts(counts["kept_dirty"])
    _emit_orphan_alert(counts["orphaned"], orphan_clones)
    return counts


def _evict_dirty(wt: str, age_s: float, counts: dict[str, int]) -> None:
    """Stash + force-remove a dirty worktree that has exceeded the max-age window."""
    age_days = age_s / 86400.0
    stash_sha = _git_stash(wt)
    if stash_sha:
        log.info(
            "worktree-janitor: stashed max-age dirty path=%s stash=%s age_days=%.1f",
            wt, stash_sha, age_days,
        )
    else:
        log.warning(
            "worktree-janitor: stash failed, discarding path=%s age_days=%.1f",
            wt, age_days,
        )

    # After a successful stash the worktree is clean; after a failed stash we
    # use force=True to discard changes (7+ days with no active session).
    removed = remove_worktree(wt, force=(stash_sha is None))
    if removed:
        counts["evicted_dirty"] += 1
        counts["removed"] += 1
    else:
        counts["kept_error"] += 1


def _emit_orphan_alert(orphaned: int, clones: set[str]) -> None:
    """Name a deleted shared clone loudly, with the command that recovers it.

    One CRITICAL line per sweep rather than per worktree: the 2026-09-03
    incident orphaned 61 worktrees on one host from a single deletion, and 61
    identical lines would bury the one fact that matters — which clone is gone.
    """
    if not orphaned:
        return
    log.critical(
        "worktree-janitor: orphaned=%d worktree(s) reference missing clone(s) %s — "
        "working files are intact; recover with `ccswitch clones repair`",
        orphaned, ", ".join(sorted(clones)),
    )


def _emit_dirty_alerts(kept_dirty: int) -> None:
    if kept_dirty > _DIRTY_CRITICAL_THRESHOLD:
        log.critical(
            "worktree-janitor: kept_dirty=%d exceeds critical threshold %d — "
            "inode exhaustion risk; review backlog-runner sessions",
            kept_dirty, _DIRTY_CRITICAL_THRESHOLD,
        )
    elif kept_dirty > _DIRTY_WARN_THRESHOLD:
        log.warning(
            "worktree-janitor: kept_dirty=%d exceeds warning threshold %d — "
            "dirty worktrees accumulating",
            kept_dirty, _DIRTY_WARN_THRESHOLD,
        )
