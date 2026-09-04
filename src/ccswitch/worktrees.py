"""Per-session git worktrees so concurrent sessions on one repo don't collide.

Each ccswitch session that selects a repo + branch runs in its OWN git worktree
checked out to that branch, instead of mutating the user's real checkout with a
`git checkout`.  This means:

  * The user's real `~/GitHub/<repo>` working tree is never touched (its HEAD,
    branch, and uncommitted files stay exactly as they were).
  * Two sessions on the same repo can be on different branches at once — each has
    a separate working tree, so there is no collision.

Worktrees live under ``<repo_root>/.ccswitch/worktrees/<repo>/<short-session>``
(a ccswitch-owned namespace next to the user's repos), and share the original
repo's object store (cheap — no full clone).

If the selected branch is already checked out somewhere (commonly the user's real
checkout sitting on `main`), git refuses to check it out twice, so we fall back to
a per-session child branch ``ccswitch/<branch>-<short>`` based on it — still
isolated, still writable, and clearly named.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from ccswitch.gitrepos import git_env_for_remote

log = logging.getLogger(__name__)

# Per-clone serialization. Every worktree creation for one shared clone mutates
# that clone's `.git` (fetch writes refs; `worktree add -b` writes upstream
# tracking into the shared `.git/config`). Git guards `.git/config` with a
# `.git/config.lock` file and FAILS IMMEDIATELY — it does not wait — when the
# lock is held, so a burst of concurrent creations against one clone loses
# all-but-one to "could not lock config file .git/config: File exists" (the
# pr-doctor multi-trigger spawn_failure). Serializing on the clone's toplevel
# path lets different repos stay parallel while same-clone creations queue.
# Keyed by repo_top (absolute clone toplevel). threading.Lock (not asyncio):
# create_worktree runs in asyncio.to_thread worker threads.
_CLONE_LOCKS: dict[str, threading.Lock] = {}
_CLONE_LOCKS_GUARD = threading.Lock()


def _clone_lock(repo_top: str) -> threading.Lock:
    """Return the process-wide lock serializing git-mutating work for *repo_top*."""
    with _CLONE_LOCKS_GUARD:
        lock = _CLONE_LOCKS.get(repo_top)
        if lock is None:
            lock = _CLONE_LOCKS[repo_top] = threading.Lock()
        return lock


class WorktreeError(Exception):
    """Raised when a worktree could not be created."""


class DiskSpaceError(WorktreeError):
    """Raised when the worktree filesystem lacks byte or inode headroom.

    Subclasses WorktreeError so existing catch-sites keep working; the
    distinct type (and the stable message marker) lets callers detect
    disk exhaustion and stop retrying instead of churning.
    """


class BranchRefUnresolvableError(WorktreeError):
    """Raised when the requested branch resolves to no ref at all — git's
    "fatal: invalid reference": not a local branch and not on origin.

    Typical cause is a lost race, not a broken repo: the branch was a PR head
    the remote auto-deleted on merge between session enqueue and spawn, and
    the pre-add fetch pruned the remote-tracking ref too (issue #836). The
    distinct type lets the orchestrator pair it with a merged PR and treat
    the failure as a benign completion; subclasses WorktreeError so existing
    catch-sites keep working.
    """


# Single source of truth for "this failure is disk exhaustion" detection.
# The git ENOSPC string appears in captured stderr of a failed checkout;
# the headroom string is ours. server/automations/backlog_runner.py imports
# this to trip its spawn circuit breaker.
DISK_EXHAUSTION_MARKERS: tuple[str, ...] = (
    "No space left on device",
    "insufficient disk headroom",
)

# Floors for the preflight. A KinDash-sized checkout is ~1.5 GB / ~19k inodes;
# defaults leave room for one checkout plus concurrent session writes.
_MIN_FREE_BYTES_DEFAULT = 2 * 1024**3      # 2 GiB
_MIN_FREE_INODES_DEFAULT = 100_000


def check_disk_headroom(
    path: str | os.PathLike[str],
    min_free_bytes: int | None = None,
    min_free_inodes: int | None = None,
) -> None:
    """Raise DiskSpaceError if the filesystem at `path` is near exhaustion.

    Checks BOTH bytes and inodes: the 2026-07-21 incident was inode
    exhaustion with 24 GB of bytes free, invisible to byte-only checks.
    Filesystems without inode accounting (f_files == 0, e.g. btrfs) skip
    the inode check. Overridable via CCSWITCH_MIN_FREE_BYTES /
    CCSWITCH_MIN_FREE_INODES.
    """
    if min_free_bytes is None:
        min_free_bytes = int(os.environ.get("CCSWITCH_MIN_FREE_BYTES", _MIN_FREE_BYTES_DEFAULT))
    if min_free_inodes is None:
        min_free_inodes = int(os.environ.get("CCSWITCH_MIN_FREE_INODES", _MIN_FREE_INODES_DEFAULT))
    st = os.statvfs(path)
    free_bytes = st.f_frsize * st.f_bavail
    if free_bytes < min_free_bytes:
        raise DiskSpaceError(
            f"insufficient disk headroom at {os.fspath(path)}: "
            f"{free_bytes} bytes free < {min_free_bytes} required")
    if st.f_files > 0 and st.f_favail < min_free_inodes:
        raise DiskSpaceError(
            f"insufficient disk headroom at {os.fspath(path)}: "
            f"{st.f_favail} inodes free < {min_free_inodes} required")


@dataclass
class WorktreeInfo:
    path: str             # the worktree directory — use this as the session cwd
    branch: str | None    # the branch checked out, or None for a detached worktree
    is_child: bool        # True if we created a child branch (selected branch was busy)
    repo: str             # absolute toplevel of the main repo the worktree belongs to


def _run(
    args: list[str], env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, env=env)


def _origin_url(repo_top: str) -> str:
    """The repo's ``origin`` URL, or "" when it has no origin."""
    r = _run(["git", "-C", repo_top, "remote", "get-url", "origin"])
    return r.stdout.strip() if r.returncode == 0 else ""


def _has_origin(repo_top: str) -> bool:
    """True if the repo has an `origin` remote configured."""
    return bool(_origin_url(repo_top))


def _remote_default_ref(repo_top: str) -> str | None:
    """Return the remote default branch ref (e.g. 'origin/master'), or None if it
    cannot be resolved.

    Repairs origin/HEAD with `set-head --auto` first in case the clone never
    recorded it. Returning None (rather than raising) lets the caller fall back to
    the local ref instead of aborting the session.
    """
    _run(["git", "-C", repo_top, "remote", "set-head", "origin", "--auto"])
    r = _run(["git", "-C", repo_top, "symbolic-ref", "--short", "refs/remotes/origin/HEAD"])
    if r.returncode != 0 or not r.stdout.strip():
        return None
    return r.stdout.strip()


def _try_fetch_origin(repo_top: str) -> bool:
    """Fetch `origin`; return True on success.

    Authenticates with the same per-owner App installation token as
    ``gitrepos.ensure_clone`` (issue #887) — this is a NETWORK call, and leaving it
    on the machine's ambient credential helper is what made "private repos with no
    cached credentials" look like an unavoidable fact rather than a bug. Falls back
    to ambient auth when no App token is available (CLI usage, non-GitHub remote).

    A fetch failure is still best-effort, NOT fatal: the credential-less deploy box
    and plain offline both hit it. On failure we log a warning and return False so
    the caller bases the session on the local ref rather than refusing to start it.
    """
    r = _run(["git", "-C", repo_top, "fetch", "origin"],
             env=git_env_for_remote(_origin_url(repo_top)))
    if r.returncode != 0:
        log.warning("worktree.fetch_failed repo=%s — basing on local ref: %s",
                    repo_top, r.stderr.strip())
        return False
    return True


def _branch_on_origin(repo_top: str, branch: str) -> bool:
    """True if `branch` exists on origin (refs/remotes/origin/<branch>)."""
    r = _run(["git", "-C", repo_top, "rev-parse", "--verify", "--quiet",
              f"refs/remotes/origin/{branch}"])
    return r.returncode == 0


def is_git_repo(path: str) -> bool:
    """True if `path` is inside a git work tree."""
    r = _run(["git", "-C", path, "rev-parse", "--is-inside-work-tree"])
    return r.returncode == 0 and r.stdout.strip() == "true"


def current_branch(path: str) -> str | None:
    """Return the branch currently checked out at `path`, or None.

    None when `path` is not a git work tree or is in detached-HEAD state
    (``git branch --show-current`` prints nothing when detached). Used to
    backfill a session's PR-join branch for sessions created before the branch
    was persisted on the session record.
    """
    r = _run(["git", "-C", path, "branch", "--show-current"])
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
_REFLOG_CHECKOUT_RE = re.compile(r"^checkout: moving from (\S+) to (\S+)$")
# Branches that are never a session's authored PR branch.
_NON_AUTHORED = {"master", "main", "HEAD"}


def is_remote_ref(name: str) -> bool:
    """True for remote-tracking ref names (``origin/foo``, ``remotes/origin/foo``).

    Reflog checkout entries can name remote refs (``git checkout origin/main``
    records "moving from … to origin/main"); a PR headRefName never carries the
    remote prefix, so such names are never pollable PR branches — they only
    bloat the PR poller's batched GraphQL query.
    """
    return name.startswith(("origin/", "remotes/"))


def authored_branches(cwd: str, scratch_branch: str | None = None) -> list[str]:
    """Sorted, deduped feature branches this worktree has checked out.

    Unions the live current branch with the worktree's reflog checkout history
    (``checkout: moving from A to B``). Filters the session's scratch branch, the
    repo default (master/main), HEAD, and bare commit SHAs (detached checkouts).
    Returns [] for a non-git dir. The caller's PR query keeps only branches that
    actually have a PR, so listing a no-PR feature branch here is harmless.
    """
    branches: set[str] = set()
    cur = current_branch(cwd)
    if cur:
        branches.add(cur)
    r = _run(["git", "-C", cwd, "reflog", "show", "--format=%gs", "HEAD"])
    if r.returncode == 0:
        for line in r.stdout.splitlines():
            m = _REFLOG_CHECKOUT_RE.match(line.strip())
            if m:
                branches.add(m.group(1))
                branches.add(m.group(2))
    skip = _NON_AUTHORED | {scratch_branch}
    return sorted(
        b for b in branches
        if b and b not in skip and not _SHA_RE.match(b) and not is_remote_ref(b)
    )


def worktrees_base(repo_root: str) -> Path:
    """ccswitch-owned root for all session worktrees, under the user's repo root."""
    return Path(repo_root).expanduser() / ".ccswitch" / "worktrees"


def is_managed_path(path: str, repo_root: str) -> bool:
    """True if `path` is one of ccswitch's managed worktree directories.

    Used at cleanup time to recognise ccswitch-created worktrees even across a
    server restart (no DB column needed — the location is the marker).
    """
    try:
        Path(path).resolve().relative_to(worktrees_base(repo_root).resolve())
        return True
    except ValueError:
        return False


def _short(session_id: str) -> str:
    # First UUID segment is plenty to disambiguate concurrent sessions.
    return session_id.split("-")[0] or session_id


def _registered_worktrees(repo_top: str) -> set[str]:
    """Absolute paths of every worktree git currently has an admin record for."""
    r = _run(["git", "-C", repo_top, "worktree", "list", "--porcelain"])
    paths: set[str] = set()
    if r.returncode == 0:
        for line in r.stdout.splitlines():
            if line.startswith("worktree "):
                paths.add(os.path.abspath(line[len("worktree "):].strip()))
    return paths


def clear_stale_registration(repo_top: str, path: str | Path) -> None:
    """Drop ONLY the git admin record for `path`, scoped to that one worktree.

    Replaces the repo-global ``git worktree prune``, which reaped a *sibling*
    session's live worktree whenever git deemed it prunable at that instant
    (issue #177). We touch exactly `path`: if git still has a registration for it
    AND its working dir is gone, force-remove just that one record. ``-f -f``
    clears even a *locked* missing record (session worktrees are locked on
    create). A still-present dir is left alone — never destroy a live tree; the
    caller's ``worktree add`` will instead surface the collision. No other
    session is ever considered.

    Used on two paths that both leave a stale registration behind: create-time
    reuse of a crashed session's dest, and archived-session purge (which rmtree's
    the working dir rather than removing it via git).
    """
    p = Path(path)
    if os.path.abspath(str(p)) not in _registered_worktrees(repo_top):
        return
    if p.exists():
        return
    _run(["git", "-C", repo_top, "worktree", "remove", "-f", "-f", str(p)])


def lock_worktree(repo_top: str, dest: Path) -> None:
    """Lock a session worktree so a sibling session's global ``git worktree
    prune`` can never reap its admin record mid-session (#177).

    Public because recovery re-locks too: :mod:`ccswitch.clone_doctor` re-creates
    admin records for worktrees orphaned by a deleted clone (#1003), and a
    recovered record must carry the same protection a freshly-created one does.

    Best-effort: a lock failure is logged, not raised — the worktree is fully
    usable unlocked, just less protected against a concurrent prune.
    """
    r = _run(["git", "-C", repo_top, "worktree", "lock", "--reason",
              "ccswitch live session", str(dest)])
    if r.returncode != 0:
        log.warning("worktree.lock_failed path=%s err=%s", dest, r.stderr.strip())


def create_worktree(repo_path: str, branch: str | None, session_id: str, repo_root: str) -> WorktreeInfo:
    """Create an isolated per-session worktree and return its info.

    When `branch` is given, checks it out directly; if it is already checked out
    elsewhere, creates a child branch ``ccswitch/<branch>-<short>`` instead. When
    `branch` is None, creates a DETACHED worktree off the repo's current HEAD — used
    for repo sessions that didn't pick a branch, so every session still gets its own
    isolated working directory (never sharing the clone) without needing a branch.

    Raises:
        WorktreeError: if `repo_path` is not a git repo, or worktree creation fails.
        BranchRefUnresolvableError: when `branch` resolves to no ref anywhere
            (e.g. a PR head branch auto-deleted on merge — issue #836).
    """
    # Argument-injection guard: reject branch names starting with '-' so they
    # cannot be interpreted as git options even with the '--' separator below.
    if branch is not None and branch.startswith("-"):
        raise WorktreeError(f"invalid branch name: {branch!r}")

    top = _run(["git", "-C", repo_path, "rev-parse", "--show-toplevel"])
    if top.returncode != 0:
        raise WorktreeError(f"not a git repo: {repo_path}")
    repo_top = top.stdout.strip()
    repo_name = os.path.basename(repo_top.rstrip("/"))

    dest = worktrees_base(repo_root) / repo_name / _short(session_id)
    dest.parent.mkdir(parents=True, exist_ok=True)

    # Serialize the git-mutating work per shared clone: fetch writes refs and
    # `worktree add -b` writes upstream tracking into the clone's `.git/config`,
    # and git fails immediately (never waits) if another process holds
    # `.git/config.lock`. Without this, concurrent creations against ONE clone
    # lose all-but-one to "could not lock config file .git/config: File exists".
    # Different clones use different locks, so unrelated repos stay parallel.
    with _clone_lock(repo_top):
        # Fail fast on a near-full filesystem BEFORE any git churn: a doomed
        # checkout writes thousands of files, fails ENOSPC, and the write+delete
        # cycle's page cache pins the daemon cgroup at MemoryHigh (2026-07-21
        # restart-loop incident). DiskSpaceError's marker string lets the backlog
        # runner stop retrying instead of looping.
        check_disk_headroom(dest.parent)

        # Clear ONLY a stale record at our own `dest` (a prior crash that reused this
        # short-session id) so the path can be reused. NOT a repo-global prune: that
        # reaped a sibling session's live worktree (issue #177).
        clear_stale_registration(repo_top, dest)

        # Fetch-first (best-effort): a session should start from the true remote tip,
        # so when origin is configured we fetch and (below) base on remote refs. But a
        # fetch failure (offline, no non-interactive credentials, unreachable origin)
        # must NOT block the session — we warn and fall back to the local ref. A repo
        # with NO origin has nothing to refresh and uses the local ref directly.
        # `use_remote` is true only when we have origin AND the fetch actually succeeded.
        has_origin = _has_origin(repo_top)
        use_remote = has_origin and _try_fetch_origin(repo_top)

        # 0) No branch selected → detached worktree. When we fetched origin, detach at
        # the remote default tip (origin/HEAD); otherwise (no origin, fetch failed, or
        # origin/HEAD unresolvable) off the local current HEAD.
        if branch is None:
            base = _remote_default_ref(repo_top) if use_remote else None
            if base is not None:
                rd = _run(["git", "-C", repo_top, "worktree", "add", "--detach",
                           str(dest), "--", base])
            else:
                rd = _run(["git", "-C", repo_top, "worktree", "add", "--detach", str(dest)])
            if rd.returncode == 0:
                lock_worktree(repo_top, dest)
                log.info("worktree.create_detached repo=%s path=%s", repo_name, dest)
                return WorktreeInfo(str(dest), None, is_child=False, repo=repo_top)
            raise WorktreeError(f"git worktree add --detach failed: {rd.stderr.strip()}")

        # 1) Remote-backed branch → isolated per-session child branch based at the
        # remote tip origin/<branch>. This is the normal path (not just a fallback):
        # a fresh, writable, isolated branch without moving the user's local <branch>.
        if use_remote and _branch_on_origin(repo_top, branch):
            child = f"ccswitch/{branch}-{_short(session_id)}"
            r = _run(["git", "-C", repo_top, "worktree", "add", "-b", child,
                      str(dest), "--", f"origin/{branch}"])
            if r.returncode == 0:
                lock_worktree(repo_top, dest)
                log.info("worktree.create_child repo=%s base=origin/%s child=%s path=%s",
                         repo_name, branch, child, dest)
                return WorktreeInfo(str(dest), child, is_child=True, repo=repo_top)
            raise WorktreeError(
                f"git worktree add failed for origin/{branch}: {r.stderr.strip()}")

        # 2) Local-only branch (or no origin) → check out the local branch directly;
        # '--' separates options from the commit-ish so a crafted branch name cannot
        # be parsed as a git option (belt-and-suspenders with the '-' prefix guard above).
        r = _run(["git", "-C", repo_top, "worktree", "add", str(dest), "--", branch])
        if r.returncode == 0:
            lock_worktree(repo_top, dest)
            log.info("worktree.create repo=%s branch=%s path=%s", repo_name, branch, dest)
            return WorktreeInfo(str(dest), branch, is_child=False, repo=repo_top)

        # 3) Branch is busy (checked out elsewhere) — make a session child branch off it.
        child = f"ccswitch/{branch}-{_short(session_id)}"
        r2 = _run(["git", "-C", repo_top, "worktree", "add", "-b", child, str(dest), "--", branch])
        if r2.returncode == 0:
            lock_worktree(repo_top, dest)
            log.info("worktree.create_child repo=%s base=%s child=%s path=%s",
                     repo_name, branch, child, dest)
            return WorktreeInfo(str(dest), child, is_child=True, repo=repo_top)

        msg = (f"git worktree add failed for {branch!r}: "
               f"{r.stderr.strip()} / {r2.stderr.strip()}")
        # r2 is the deciding attempt: `-b <child> -- <branch>` needs only that
        # <branch> resolves as a commit-ish, so "invalid reference" there means
        # the ref exists nowhere (deleted-on-merge race), not merely "busy".
        if "invalid reference" in r2.stderr:
            raise BranchRefUnresolvableError(msg)
        raise WorktreeError(msg)


def remove_worktree(worktree_path: str, force: bool = False) -> bool:
    """Remove a ccswitch worktree IFF it has no uncommitted changes.

    Returns True if removed, False if it was left in place (dirty — we never
    destroy uncommitted work). Also deletes the per-session ``ccswitch/*`` child
    branch when one was created, so the repo stays tidy.

    ``force=True`` bypasses the dirty guard and uses ``git worktree remove
    --force``.  Only the janitor's max-age eviction path should set this —
    normal teardown must never discard uncommitted work.
    """
    wt = os.path.abspath(worktree_path)
    if not os.path.isdir(wt):
        return False

    # Resolve the main repo this worktree belongs to (its common git dir is
    # <repo>/.git; the worktree's own .git is just a pointer file).
    common = _run(["git", "-C", wt, "rev-parse", "--path-format=absolute", "--git-common-dir"])
    if common.returncode != 0:
        return False
    repo_top = os.path.dirname(common.stdout.strip().rstrip("/"))

    # Never blow away uncommitted work (unless the caller explicitly forces eviction).
    status = _run(["git", "-C", wt, "status", "--porcelain"])
    is_dirty = status.returncode == 0 and bool(status.stdout.strip())
    if is_dirty and not force:
        log.info("worktree.keep_dirty path=%s", wt)
        return False

    branch_r = _run(["git", "-C", wt, "rev-parse", "--abbrev-ref", "HEAD"])
    branch = branch_r.stdout.strip() if branch_r.returncode == 0 else ""

    # Session worktrees are locked on create (#177); `git worktree remove` refuses
    # a locked tree, so unlock first. Harmless no-op for an older unlocked worktree.
    _run(["git", "-C", repo_top, "worktree", "unlock", wt])
    rm_cmd = ["git", "-C", repo_top, "worktree", "remove"]
    if is_dirty:
        rm_cmd.append("--force")
    rm_cmd.append(wt)
    rm = _run(rm_cmd)
    if rm.returncode != 0:
        log.warning("worktree.remove_failed path=%s err=%s", wt, rm.stderr.strip())
        return False

    # Tidy up the session child branch we created (only the ccswitch/* ones).
    # `remove` already deregisters this worktree; no global prune (it could reap a
    # sibling session's live worktree — #177).
    if branch.startswith("ccswitch/"):
        _run(["git", "-C", repo_top, "branch", "-D", branch])
    log.info("worktree.remove path=%s branch=%s force=%s", wt, branch, force)
    return True
