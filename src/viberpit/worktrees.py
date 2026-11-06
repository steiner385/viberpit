"""Per-session git worktrees so concurrent sessions on one repo don't collide.

Each ViberPit session that selects a repo + branch runs in its OWN git worktree
checked out to that branch, instead of mutating the user's real checkout with a
`git checkout`.  This means:

  * The user's real `~/GitHub/<repo>` working tree is never touched (its HEAD,
    branch, and uncommitted files stay exactly as they were).
  * Two sessions on the same repo can be on different branches at once — each has
    a separate working tree, so there is no collision.

New worktrees live under ``<repo_root>/viberpit-sessions/worktrees/<repo>/<short-session>``
(a ViberPit-owned, deliberately NOT hidden dir next to the user's repos —
``paths.session_namespace_dir``, #2934; older ones stay under the dot-dir namespace
they were created in), and share the original repo's object store (cheap — no full clone).

If the selected branch is already checked out somewhere (commonly the user's real
checkout sitting on `main`), git refuses to check it out twice, so we fall back to
a per-session child branch ``<prefix><branch>-<short>`` based on it — still
isolated, still writable, and clearly named. ``<prefix>`` is the configured
session-branch prefix (D-22; built-in ``viberpit/``). No name decides deletion:
``remove_worktree`` deletes a branch only when its required ``owned_branch``
predicate — the server's recorded-ownership check — says ViberPit created it.
"""
from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import brand, gh_credential, paths
from .gitrepos import STALE_LOCK_AGE_S, clone_lock, git_env_for_remote
from .fs_beneath import SymlinkInPath, open_beneath, open_dir_beneath
from .worktree_diff import (
    NO_SUBMODULE_RECURSE,
    GitTimeout,
    UnsafeGitConfig,
    filter_overrides,
    merge_driver_overrides,
    run_git,
    untrusted_conditional_includes,
    worktree_top,
)

log = logging.getLogger(__name__)


def _log_fields(level: int, event: str, **fields: object) -> None:
    """A structured record (the ``server.obs`` shape — ``event`` + ``fields`` extras —
    without importing it): the message is the bare event name, so the error sink's
    fingerprint does not change with a path or a branch name."""
    log.log(level, event, extra={"event": event, "fields": fields})

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
    the inode check. Overridable via VIBERPIT_MIN_FREE_BYTES /
    VIBERPIT_MIN_FREE_INODES.
    """
    if min_free_bytes is None:
        min_free_bytes = int(os.environ.get("VIBERPIT_MIN_FREE_BYTES", _MIN_FREE_BYTES_DEFAULT))
    if min_free_inodes is None:
        min_free_inodes = int(os.environ.get("VIBERPIT_MIN_FREE_INODES", _MIN_FREE_INODES_DEFAULT))
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
    is_child: bool        # True if this call created the branch it checked out
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
        # An unreachable host is the expected offline case, not a defect (#2610).
        level = (logging.INFO
                 if gh_credential.classify_gh_failure(r.stderr) == gh_credential.UNREACHABLE
                 else logging.WARNING)
        log.log(level, "worktree.fetch_failed repo=%s — basing on local ref: %s",
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


def git_dir_of(cwd: str) -> tuple[str, str] | None:
    """``(worktree_top, git_dir)`` for *cwd* without spawning git, or None.

    Walks up from *cwd* to the first ``.git``: a directory is a normal checkout's
    git dir; a file is a linked worktree's ``gitdir: <path>`` pointer (relative
    paths resolve against the file's directory).
    """
    cur = os.path.realpath(cwd)
    if not os.path.isdir(cur):
        return None
    while True:
        dot_git = os.path.join(cur, ".git")
        if os.path.isdir(dot_git):
            return cur, os.path.realpath(dot_git)
        if os.path.isfile(dot_git):
            with open(dot_git, encoding="utf-8") as fh:
                first = fh.readline().strip()
            if not first.startswith("gitdir:"):
                return None
            target = first[len("gitdir:"):].strip()
            git_dir = os.path.realpath(os.path.join(cur, target))
            return (cur, git_dir) if os.path.isdir(git_dir) else None
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def _git_pids_in(*paths: str) -> set[int] | None:
    """PIDs of live ``git`` processes whose cwd is inside any of *paths*.

    None when ``/proc`` itself cannot be listed — the answer is unknowable, and the
    caller must then keep the lock. A pid that vanished or is not ours to inspect is
    skipped (a foreign-uid git cannot be holding our session worktree's index).
    """
    roots = [os.path.realpath(p) for p in paths]
    try:
        entries = os.listdir("/proc")
    except OSError:
        return None
    found: set[int] = set()
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/comm", encoding="utf-8") as fh:
                if fh.read().strip() != "git":
                    continue
            proc_cwd = os.path.realpath(os.readlink(f"/proc/{entry}/cwd"))
        except (PermissionError, FileNotFoundError, ProcessLookupError):
            continue
        if any(proc_cwd == r or proc_cwd.startswith(r + os.sep) for r in roots):
            found.add(int(entry))
    return found


def clear_stale_index_lock(cwd: str, *, min_age_s: float = STALE_LOCK_AGE_S) -> str | None:
    """Unlink an orphaned ``<gitdir>/index.lock`` for the worktree at *cwd* (#2910).

    A daemon restart can kill a git process mid-commit inside a session worktree;
    git never removes a dead process's lock, so every later commit fails. The lock
    is removed only when it is a regular file (not a symlink), older than
    *min_age_s*, and no live ``git`` process has its cwd inside the worktree or its
    git dir. Returns the removed path, or None. Never raises.
    """
    try:
        resolved = git_dir_of(cwd)
        if resolved is None:
            return None
        top, git_dir = resolved
        lock = os.path.join(git_dir, "index.lock")
        if os.path.islink(lock) or not os.path.isfile(lock):
            return None
        if time.time() - os.stat(lock).st_mtime < min_age_s:
            return None
        owners = _git_pids_in(top, git_dir)
        if owners is None or owners:
            return None
        os.unlink(lock)
    except (OSError, ValueError):  # ValueError: NUL in a path, undecodable .git file
        return None
    log.info("worktree.stale_index_lock_removed path=%s", lock)
    return lock


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



def verified_created_worktree_branch(cwd: str, path: str, branch: str,
                                    started_ms: float | None,
                                    finished_ms: float | None) -> str | None:
    """Verify explicit worktree-add evidence against Git's repository and reflog.

    A compound shell's success alone cannot establish that worktree creation ran.
    Require the actual new branch's creation to fall within the tool's timestamped
    interval; a pre-existing sibling worktree must never become this session's work.
    """
    if (started_ms is None or finished_ms is None or finished_ms < started_ms
            or not os.path.isabs(path) or branch.startswith("-")):
        return None
    common = []
    for location in (cwd, path):
        result = _run(["git", "-C", location, "rev-parse", "--path-format=absolute", "--git-common-dir"])
        if result.returncode != 0:
            return None
        common.append(os.path.realpath(result.stdout.strip()))
    if common[0] != common[1] or current_branch(path) != branch:
        return None
    result = _run(["git", "-C", path, "reflog", "show", "--date=unix",
                   "--format=%gD%x09%gs", "refs/heads/" + branch])
    if result.returncode != 0:
        return None
    lines = result.stdout.splitlines()
    if not lines:
        return None
    created = re.search(r"@\{(\d+)\}\tbranch: Created from ", lines[-1])
    # Reflog timestamps have whole-second precision; include that same second.
    if created and int(started_ms // 1000) <= int(created.group(1)) <= int(finished_ms // 1000):
        return branch
    return None

def push_evidence(cwd: str) -> tuple[str | None, str | None]:
    """``(upstream_branch, head_sha)`` for the worktree at ``cwd``, each None when
    unknown. ``upstream_branch`` is the ``origin`` branch HEAD's branch tracks, with
    the remote prefix stripped — the branch a ``git push -u`` actually wrote, which
    need not match the local branch name (#2909). Local refs only, no network."""
    r = _run(["git", "-C", cwd, "rev-parse", "--abbrev-ref", "--symbolic-full-name",
              "@{upstream}"])
    upstream = r.stdout.strip() if r.returncode == 0 else ""
    branch = upstream[len("origin/"):] if upstream.startswith("origin/") else None
    r = _run(["git", "-C", cwd, "rev-parse", "HEAD"])
    sha = r.stdout.strip() if r.returncode == 0 else ""
    return (branch or None, sha if _SHA_RE.match(sha) else None)


def commits_ahead_of_default(cwd: str) -> int | None:
    """Commits on ``cwd``'s HEAD that origin's default branch does not have, or None
    when that cannot be told (not a git dir, no recorded ``origin/HEAD``). Local refs
    only — no fetch, no ``set-head --auto``: this is read on a completion, not at
    worktree creation, and must not touch the network."""
    r = _run(["git", "-C", cwd, "symbolic-ref", "--short", "refs/remotes/origin/HEAD"])
    base = r.stdout.strip() if r.returncode == 0 else ""
    if not base:
        return None
    r = _run(["git", "-C", cwd, "rev-list", "--count", f"{base}..HEAD"])
    if r.returncode != 0:
        return None
    try:
        return int(r.stdout.strip())
    except ValueError:
        return None


def worktrees_base(repo_root: str) -> Path:
    """ViberPit-owned root NEW session worktrees are created under — the
    non-hidden ``paths.session_namespace_dir`` (#2934)."""
    return paths.session_namespace_dir(repo_root) / "worktrees"


def worktrees_bases(repo_root: str) -> tuple[Path, ...]:
    """Every worktrees root a reader must scan — every namespace spelling (#1373, #2934)."""
    return tuple(ns / "worktrees" for ns in paths.repo_namespace_dirs(repo_root))


def _is_under_any(path: str, bases: tuple[Path, ...]) -> bool:
    resolved = Path(path).resolve()
    for base in bases:
        try:
            resolved.relative_to(base.resolve())
            return True
        except ValueError:
            continue
    return False


def is_managed_path(path: str, repo_root: str) -> bool:
    """True if `path` is one of ViberPit's managed worktree directories.

    Used at cleanup time to recognise ViberPit-created worktrees even across a
    server restart (no DB column needed — the location is the marker).
    """
    return _is_under_any(path, worktrees_bases(repo_root))


def scratch_base(repo_root: str) -> Path:
    """ViberPit-owned root for per-session scratch dirs (no-repo sessions).

    Sibling of ``worktrees_base``, under the same ViberPit-owned parent.
    Single source of truth for the path both create_session (scratch dir
    creation) and session teardown (scratch dir removal) must agree on.
    """
    return worktrees_base(repo_root).parent / "scratch"


def is_scratch_path(path: str, repo_root: str) -> bool:
    """True if `path` sits under a scratch base of either namespace spelling."""
    return _is_under_any(
        path, tuple(ns / "scratch" for ns in paths.repo_namespace_dirs(repo_root)))


def remove_scratch_dir(scratch_path: str) -> bool:
    """Remove a ViberPit-managed scratch directory. Returns True if removed.

    Unlike ``remove_worktree`` there is no dirty check — a scratch dir is a
    plain directory, not a git worktree, so there is no uncommitted work to
    protect. The caller is responsible for confirming *scratch_path* actually
    falls under ``scratch_base(repo_root)`` before calling this: it performs
    the delete, it does not scope it.
    """
    p = Path(scratch_path)
    if not p.is_dir():
        return False
    shutil.rmtree(str(p))
    return True


def _short(session_id: str) -> str:
    # First UUID segment is plenty to disambiguate concurrent sessions.
    return session_id.split("-")[0] or session_id


def session_worktree_dir(repo_root: str, repo_top: str, session_id: str) -> Path:
    """Where session *session_id*'s worktree of the clone at *repo_top* lives:
    ``<worktrees_base>/<clone name>/<short id>``. The ONE layout — new sessions and
    forks are both created here, and the janitor (``_candidate_worktree_dirs``)
    sweeps exactly this shape."""
    return worktrees_base(repo_root) / os.path.basename(repo_top.rstrip("/")) / _short(session_id)


#: Path components below a worktrees base in ``session_worktree_dir``'s layout:
#: ``<clone name>/<short id>``.
_WORKTREE_DEPTH = 2


def session_worktree_root(path: str, repo_root: str) -> Path | None:
    """The managed session worktree (``session_worktree_dir``'s
    ``<worktrees_base>/<clone>/<short id>``, either namespace spelling) that contains
    *path*, resolved — or None for a path outside every worktrees base or above the
    worktree depth (the base itself, or a ``<clone>`` grouping dir). Resolves *path*
    (stats it): call it on session cwds, not on arbitrary process cwds."""
    resolved = Path(path).resolve()
    for base in worktrees_bases(repo_root):
        try:
            rel = resolved.relative_to(base.resolve())
        except ValueError:
            continue
        if len(rel.parts) >= _WORKTREE_DEPTH:
            return base.resolve().joinpath(*rel.parts[:_WORKTREE_DEPTH])
        return None
    return None


def _child_branch(base: str, session_id: str, prefix: str = brand.BRANCH_PREFIX) -> str:
    """Name of the per-session child branch created off *base*: ``<prefix><base>-<short>``.

    One function rather than the two identical f-strings create_worktree used to
    carry. *prefix* is the configured session-branch prefix (D-22; the server resolves
    it), defaulting to the built-in one. The name is never the deletion gate — a
    branch is deleted only through its recorded creation.
    """
    return f"{prefix}{base}-{_short(session_id)}"


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

    Public because recovery re-locks too: :mod:`viberpit.clone_doctor` re-creates
    admin records for worktrees orphaned by a deleted clone (#1003), and a
    recovered record must carry the same protection a freshly-created one does.

    Best-effort: a lock failure is logged, not raised — the worktree is fully
    usable unlocked, just less protected against a concurrent prune.

    Also pins the worktree's git config (:func:`pin_worktree_config`, #2935) — the
    same every-create-and-recovery chokepoint, for the same reason: a recovered
    worktree needs the protection a fresh one gets.
    """
    r = _run(["git", "-C", repo_top, "worktree", "lock", "--reason",
              f"{brand.NAME} live session", str(dest)])
    if r.returncode != 0:
        log.warning("worktree.lock_failed path=%s err=%s", dest, r.stderr.strip())
    pin_worktree_config(repo_top, dest)


#: Shared-clone config keys every session worktree pins in its own
#: ``config.worktree`` (#2935). All worktrees of a clone read ONE ``.git/config``,
#: so a husky/lefthook install (``git config core.hooksPath …``) in one session
#: swapped the hooks of every sibling, mid-commit included. A per-worktree value
#: outranks the shared one, so a pinned sibling no longer sees that write.
_PINNED_KEYS: tuple[str, ...] = ("core.hooksPath",)


def pin_worktree_config(repo_top: str, dest: Path) -> None:
    """Isolate *dest* from later writes to the clone's shared git config (#2935).

    Turns on ``extensions.worktreeConfig`` for the clone, then copies each
    ``_PINNED_KEYS`` value as it stands NOW into *dest*'s ``config.worktree``.
    An unset ``core.hooksPath`` is pinned to the clone's default hooks dir, so the
    pin means "what git would have used anyway" until the session itself changes
    it. A plain ``git config core.hooksPath X`` run in any worktree still writes
    the shared file — it just no longer reaches pinned siblings; a session that
    wants new hooks for itself uses ``git config --worktree``.

    Called from :func:`lock_worktree`, so every path that creates or recovers a
    session worktree pins it. An existing per-worktree value is left alone
    (re-locking a recovered worktree must not clobber a session's own choice).
    Skipped when the shared config sets ``core.worktree`` (enabling the extension
    would then require moving it — git's documented caveat). Best-effort: a
    failure is logged, never raised; the worktree still works, just unisolated.
    """
    if _run(["git", "-C", repo_top, "config", "--local", "--get",
             "core.worktree"]).returncode == 0:
        log.info("worktree.config_pin_skipped path=%s reason=core.worktree", dest)
        return
    ext = _run(["git", "-C", repo_top, "config", "--local", "--type=bool", "--get",
                "extensions.worktreeConfig"])
    if ext.stdout.strip() != "true":
        r = _run(["git", "-C", repo_top, "config", "--local",
                  "extensions.worktreeConfig", "true"])
        if r.returncode != 0:
            log.warning("worktree.config_pin_failed path=%s step=extension err=%s",
                        dest, r.stderr.strip())
            return
    for key in _PINNED_KEYS:
        if _run(["git", "-C", str(dest), "config", "--worktree", "--get",
                 key]).returncode == 0:
            continue
        shared = _run(["git", "-C", repo_top, "config", "--local", "--get", key])
        value = shared.stdout.strip() if shared.returncode == 0 else None
        if value is None and key == "core.hooksPath":
            common = _run(["git", "-C", repo_top, "rev-parse", "--path-format=absolute",
                           "--git-common-dir"])
            if common.returncode != 0:
                continue
            value = os.path.join(common.stdout.strip(), "hooks")
        if value is None:
            continue
        r = _run(["git", "-C", str(dest), "config", "--worktree", key, value])
        if r.returncode != 0:
            log.warning("worktree.config_pin_failed path=%s key=%s err=%s",
                        dest, key, r.stderr.strip())


def create_worktree(repo_path: str, branch: str | None, session_id: str, repo_root: str,
                    *, branch_prefix: str | None = None) -> WorktreeInfo:
    """Create an isolated per-session worktree and return its info.

    When `branch` is given, checks it out directly; if it is already checked out
    elsewhere, creates a child branch ``<branch_prefix><branch>-<short>`` instead (the
    built-in ``viberpit/`` when *branch_prefix* is None). When
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

    dest = session_worktree_dir(repo_root, repo_top, session_id)
    dest.parent.mkdir(parents=True, exist_ok=True)

    # Serialize the git-mutating work per shared clone: fetch writes refs and
    # `worktree add -b` writes upstream tracking into the clone's `.git/config`,
    # and git fails immediately (never waits) if another process holds
    # `.git/config.lock`. Without this, concurrent creations against ONE clone
    # lose all-but-one to "could not lock config file .git/config: File exists".
    # Different clones use different locks, so unrelated repos stay parallel.
    with clone_lock(repo_top):
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
            child = _child_branch(branch, session_id, branch_prefix or brand.BRANCH_PREFIX)
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
        child = _child_branch(branch, session_id, branch_prefix or brand.BRANCH_PREFIX)
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


# ---------------------------------------------------------------------------
# Fork worktrees (D-8): a child at the source's HEAD + its uncommitted work.
# ---------------------------------------------------------------------------

#: Ceiling on the untracked bytes one fork copies. Over it the fork fails (and
#: cleans up) rather than silently dropping files or filling the disk.
FORK_MAX_UNTRACKED_BYTES = 200 * 1024**2
#: Per-call ceiling for the fork's tree-sized git calls (snapshot, checkout, apply).
#: ``create_worktree``'s checkout has no ceiling at all; this one only stops a hung
#: git from holding the clone lock forever, never a large-but-healthy checkout.
FORK_GIT_TIMEOUT_S = 600.0
#: Identity for the throwaway ``stash create`` commits. They are never put on a
#: branch (only their trees are applied), and a clone with no ``user.*`` set would
#: otherwise fail "Please tell me who you are".
_FORK_IDENT = ("-c", f"user.name={brand.NAME} fork",
               "-c", f"user.email=fork@{brand.SLUG}.invalid")
_COPY_CHUNK = 1 << 20


#: ``ForkWorktreeError.code`` values — the stable reasons that cross the daemon RPC
#: (``error_data``) and that the edge maps to a 409 with the same code.
FORK_FAILED = "FORK_FAILED"
FORK_UNTRACKED_TOO_LARGE = "FORK_UNTRACKED_TOO_LARGE"
FORK_NOT_A_REPO = "FORK_NOT_A_REPO"
FORK_NO_HEAD = "FORK_NO_HEAD"
FORK_DEST_EXISTS = "FORK_DEST_EXISTS"
FORK_UNSAFE_CONFIG = "FORK_UNSAFE_CONFIG"
FORK_APPLY_FAILED = "FORK_APPLY_FAILED"
FORK_DISK_FULL = "FORK_DISK_FULL"
FORK_WORKTREE_CODES = frozenset({
    FORK_FAILED,
    FORK_UNTRACKED_TOO_LARGE,
    FORK_NOT_A_REPO,
    FORK_NO_HEAD,
    FORK_DEST_EXISTS,
    FORK_UNSAFE_CONFIG,
    FORK_APPLY_FAILED,
    FORK_DISK_FULL,
})


class ForkWorktreeError(WorktreeError):
    """A fork worktree could not be created; nothing half-made is left behind.

    ``code`` (one of ``FORK_WORKTREE_CODES``) is a stable machine-readable reason;
    ``error_data`` carries it across the daemon RPC envelope, which also marks the
    refusal expected (logged at INFO, not an auto-filed WARNING)."""

    def __init__(self, message: str, code: str = FORK_FAILED) -> None:
        super().__init__(message)
        self.code = code
        self.error_data = {"code": code}


@dataclass
class ForkWorktreeInfo:
    path: str     # the new worktree's root
    cwd: str      # the child's cwd: the source cwd's position mirrored inside ``path``
    branch: str   # the fork branch checked out in ``path``
    repo: str     # absolute toplevel of the main clone both worktrees belong to


def fork_branch_name(source_session_id: str, n: int,
                     prefix: str = brand.BRANCH_PREFIX) -> str:
    """``<prefix>fork-<short source id>-<n>``; *prefix* is the configured session-branch
    prefix (D-22), defaulting to the built-in one. Deleted only through its record."""
    return f"{prefix}fork-{_short(source_session_id)}-{n}"


def _next_fork_branch(repo_top: str, source_session_id: str,
                      prefix: str = brand.BRANCH_PREFIX) -> str:
    """The first ``fork_branch_name(source, n, prefix)`` (n ≥ 1) whose ``n`` no fork of
    this source holds under the built-in prefixes or the current *prefix*. One
    ``for-each-ref``. Call under ``clone_lock(repo_top)``.

    Exactly that, no more: an ``n`` held only under an EARLIER custom prefix is not
    seen, so after a change between two custom prefixes an ``n`` may repeat. Harmless —
    the names still differ by prefix, so git keeps both, and deletion goes through
    each branch's own record, never the name."""
    stems = ["refs/heads/" + fork_branch_name(source_session_id, 0, p)[:-1]
             for p in dict.fromkeys((prefix, *brand.BRANCH_PREFIXES))]
    out = run_git(repo_top, *NO_SUBMODULE_RECURSE, "for-each-ref", "--format=%(refname)",
                  *(stem + "*" for stem in stems))
    if out is None:
        raise ForkWorktreeError("could not list the fork branches")
    taken = {int(s) for line in os.fsdecode(out).splitlines() for stem in stems
             if line.startswith(stem) and (s := line[len(stem):]).isascii() and s.isdecimal()}
    n = 1
    while n in taken:
        n += 1
    return fork_branch_name(source_session_id, n, prefix)


def _rel_ok(rel: bytes) -> bool:
    """A git-listed relative path we are willing to recreate: no empty, ``.``/``..``
    or ``.git`` component (case-insensitively), not absolute, not a ``dir/`` entry
    (a nested repository)."""
    if not rel or rel.startswith(b"/") or rel.endswith(b"/"):
        return False
    return all(p not in (b"", b".", b"..") and p.lower() != b".git" for p in rel.split(b"/"))


def _plan_untracked(top: str, listing: bytes) -> list[bytes]:
    """Untracked entries worth copying — regular files by ``lstat`` — bounded in total
    size before the child exists. A filter only: :func:`_copy_untracked` re-checks
    everything at open time."""
    btop = os.fsencode(top)
    keep: list[bytes] = []
    total = 0
    for rel in listing.split(b"\0"):
        if not _rel_ok(rel):
            continue
        try:
            st = os.lstat(os.path.join(btop, rel))
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode):
            continue  # symlinks, FIFOs, devices, sockets
        total += st.st_size
        if total > FORK_MAX_UNTRACKED_BYTES:
            raise ForkWorktreeError(
                f"untracked files exceed the {FORK_MAX_UNTRACKED_BYTES} byte fork limit",
                FORK_UNTRACKED_TOO_LARGE)
        keep.append(rel)
    return keep


def _copy_untracked(src_top: str, dst_top: str, entries: list[bytes]) -> None:
    """Copy each planned regular file into the child, mode bits kept.

    Both sides go through :mod:`viberpit.fs_beneath` from a root directory fd, so no
    symlink is followed anywhere on either path — one swapped in after the plan
    included. The source leaf is opened non-blocking (a FIFO cannot park the worker)
    and only a regular file is read; a source path that no longer resolves cleanly
    is skipped. In the child, each parent directory is opened, or made with
    ``mkdir(dir_fd=…)``, one component at a time, so a symlinked component is refused
    before anything is created beneath it; that fails the fork. The byte bound is
    enforced again on what is actually read."""
    left = FORK_MAX_UNTRACKED_BYTES
    src_root = os.open(src_top, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        dst_root = os.open(dst_top, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except BaseException:
        os.close(src_root)
        raise
    try:
        for rel in entries:
            if not _rel_ok(rel):
                continue
            try:
                fd = open_beneath(src_root, rel)
            except OSError:
                continue  # gone, or a symlink on the way since the plan
            with os.fdopen(fd, "rb") as src:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode):
                    continue
                try:
                    pfd = open_dir_beneath(dst_root, rel.split(b"/")[:-1], create=True)
                except SymlinkInPath as exc:
                    raise ForkWorktreeError(
                        f"untracked path escapes the fork: {os.fsdecode(rel)}") from exc
                try:
                    out = os.open(os.path.basename(rel), os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                  | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=pfd)
                finally:
                    os.close(pfd)
                with os.fdopen(out, "wb") as dst:
                    while chunk := src.read(_COPY_CHUNK):
                        left -= len(chunk)
                        if left < 0:
                            raise ForkWorktreeError(
                                f"untracked files exceed the {FORK_MAX_UNTRACKED_BYTES} byte"
                                " fork limit", FORK_UNTRACKED_TOO_LARGE)
                        dst.write(chunk)
                    os.fchmod(dst.fileno(), stat.S_IMODE(st.st_mode) & 0o777)
    finally:
        os.close(src_root)
        os.close(dst_root)


def _discard_fork(repo_top: str, dest: str, branch: str) -> None:
    """Remove a half-made fork: its worktree (locked or not), admin record and branch.

    Only called once this fork's own ``worktree add`` succeeded under the clone lock,
    i.e. for a ``dest`` and ``branch`` this call created — never for one a failed
    ``add`` may have found already there."""
    _run(["git", "-C", repo_top, "worktree", "unlock", dest])
    _run(["git", "-C", repo_top, "worktree", "remove", "-f", "-f", dest])
    if os.path.lexists(dest):
        shutil.rmtree(dest, ignore_errors=True)
    clear_stale_registration(repo_top, dest)
    _run(["git", "-C", repo_top, "branch", "-D", "--", branch])


def fork_worktree_dir(source_cwd: str, repo_root: str, session_id: str) -> str:
    """The standard session worktree dir (``session_worktree_dir``) for fork child
    *session_id* of the source worktree at *source_cwd*, found without running git.

    Raises ``ForkWorktreeError(FORK_NOT_A_REPO)`` when *source_cwd* is in no git
    work tree."""
    try:
        loc = git_dir_of(source_cwd)
    except OSError:
        loc = None
    if loc is None:
        raise ForkWorktreeError(f"not a git work tree: {source_cwd}", FORK_NOT_A_REPO)
    top, git_dir = loc
    from viberpit.clone_doctor import clone_dir_from_gitdir
    clone = clone_dir_from_gitdir(git_dir) or top
    return str(session_worktree_dir(repo_root, clone, session_id))


def discard_fork_worktree(info: ForkWorktreeInfo) -> None:
    """Remove a fork worktree whose session never started: worktree, admin record and
    fork branch, dirty or not (it holds only a copy of the source's state)."""
    _discard_fork(info.repo, info.path, info.branch)


def create_fork_worktree(source_cwd: str, *, source_session_id: str,
                         dest: str, branch_prefix: str | None = None) -> ForkWorktreeInfo:
    """Create a new worktree at ``dest`` on a new fork branch, at the source worktree's
    current HEAD and carrying its uncommitted work. Read-only on the source.

    - Tracked changes, staged and unstaged: ``git stash create`` against a private
      COPY of the source index, then ``git stash apply --index`` in the child. A
      plain ``stash create`` refreshes and rewrites the source's own index; the copy
      keeps that file byte-identical. Only objects are written to the shared store,
      and no ``refs/stash`` entry is made. The stash must sit on the HEAD the child
      was made at (``stash^1``), or the fork fails closed.
    - Untracked, non-ignored files (``ls-files --others --exclude-standard``) are
      copied, regular files only, mode bits kept, at most
      ``FORK_MAX_UNTRACKED_BYTES`` in total.

    ``source_cwd`` may be a subdirectory of its work tree: the fork branches from the
    toplevel, and ``ForkWorktreeInfo.cwd`` is the same relative position in ``dest``.
    The branch is ``fork_branch_name(source_session_id, n)`` with the first free
    ``n``, picked under ``clone_lock`` together with the final check that ``dest``
    (absolute) does not exist, so two concurrent forks never share either. Any
    failure after this call's ``worktree add`` succeeded removes the worktree and
    the branch; a failed ``add`` touches nothing it may not own.

    Git runs hardened (``worktree_diff``'s ``GIT_GLOBAL``/``git_env``: no hooks, no
    fsmonitor, no system config), with repo-scope filter and merge drivers
    neutralised and submodules never recursed into: the source tree and the clone's
    config are agent-writable.

    Raises ``ForkWorktreeError`` (with a ``code``) on every failure.
    """
    if not os.path.isabs(dest):
        raise ForkWorktreeError(f"fork dest must be absolute: {dest!r}")
    try:
        top = worktree_top(source_cwd)
        if top is None:
            raise ForkWorktreeError(f"not a git work tree: {source_cwd}", FORK_NOT_A_REPO)
        rel = os.path.relpath(os.path.realpath(source_cwd), os.path.realpath(top))
        if rel == ".." or rel.startswith("../"):
            rel = "."
        common = run_git(top, "rev-parse", "--path-format=absolute", "--git-common-dir")
        head = run_git(top, "rev-parse", "--verify", "-q", "HEAD^{commit}")
        index = run_git(top, "rev-parse", "--path-format=absolute", "--git-path", "index")
        if common is None or index is None:
            raise ForkWorktreeError(f"cannot resolve the source repository: {top}")
        if head is None:
            raise ForkWorktreeError("the source has no commit to fork from", FORK_NO_HEAD)
        repo_top = os.path.dirname(os.fsdecode(common.rstrip(b"\n")).rstrip("/"))
        head_sha = os.fsdecode(head.strip())
        src_index = os.fsdecode(index.rstrip(b"\n"))
        if not os.path.isfile(src_index):
            # Without an index git reads every tracked file as a staged deletion; a
            # fork of that would be wrong, and seeding one would be a guess.
            raise ForkWorktreeError("the source has no index to fork from")
        if os.path.lexists(dest):
            raise ForkWorktreeError(f"fork dest already exists: {dest}", FORK_DEST_EXISTS)
        # The overrides below are scanned in the SOURCE's config context, but the
        # checkout and apply run in the child's (new gitdir, fork branch): an
        # ``includeIf "onbranch:…"``/``"gitdir:…"`` could add a driver only there.
        # Fail closed on any repo-scope conditional include.
        if conditional := untrusted_conditional_includes(top):
            raise ForkWorktreeError(
                f"repository config has conditional includes: {', '.join(conditional)}",
                FORK_UNSAFE_CONFIG)
        git = (*NO_SUBMODULE_RECURSE, *filter_overrides(top), *merge_driver_overrides(top))

        # Read the source: only objects are written, never its index or tree.
        with tempfile.TemporaryDirectory(prefix=f"{brand.SLUG}-fork-") as td:
            private_index = os.path.join(td, "index")
            shutil.copyfile(src_index, private_index)
            stash = run_git(top, *git, *_FORK_IDENT, "stash", "create",
                            index_file=private_index, timeout=FORK_GIT_TIMEOUT_S)
            listing = run_git(top, *git, "ls-files", "--others", "--exclude-standard", "-z",
                              index_file=private_index, timeout=FORK_GIT_TIMEOUT_S)
        if stash is None or listing is None:
            raise ForkWorktreeError("could not snapshot the source's uncommitted work")
        stash_sha = os.fsdecode(stash.strip())
        if stash_sha:
            # The child is made at head_sha and checked to be there; the stash must
            # sit on that same commit, or applying it would be a real merge.
            parent = run_git(top, *NO_SUBMODULE_RECURSE, "rev-parse", "--verify", "-q",
                             f"{stash_sha}^1")
            if parent is None or os.fsdecode(parent.strip()) != head_sha:
                raise ForkWorktreeError("the source's HEAD moved while it was being forked")
        untracked = _plan_untracked(top, listing)
    except GitTimeout as exc:
        raise ForkWorktreeError("git timed out reading the fork source") from exc
    except UnsafeGitConfig as exc:
        raise ForkWorktreeError(f"unsafe repository config: {exc}", FORK_UNSAFE_CONFIG) from exc
    except OSError as exc:
        raise ForkWorktreeError(f"could not read the fork source: {exc}") from exc

    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    with clone_lock(repo_top):
        try:
            check_disk_headroom(Path(dest).parent)
            # Re-checked under the lock: a concurrent fork may have taken either.
            if os.path.lexists(dest):
                raise ForkWorktreeError(f"fork dest already exists: {dest}",
                                        FORK_DEST_EXISTS)
            branch = _next_fork_branch(repo_top, source_session_id,
                                       branch_prefix or brand.BRANCH_PREFIX)
        except DiskSpaceError as exc:
            raise ForkWorktreeError(str(exc), FORK_DISK_FULL) from exc
        except GitTimeout as exc:
            raise ForkWorktreeError("git timed out naming the fork branch") from exc
        try:
            # Run from the SOURCE worktree, not repo_top: with extensions.worktreeConfig
            # git copies the *current* worktree's config.worktree into the new one,
            # and only the source's worktree scope is what ``git`` was scanned from.
            # From the main worktree, its unscanned config.worktree (a filter driver,
            # say) would reach the child's checkout un-neutralised.
            added = run_git(top, *git, "worktree", "add", "-q", "-b", branch,
                            dest, "--", head_sha, timeout=FORK_GIT_TIMEOUT_S)
        except GitTimeout as exc:
            # git was killed mid-add. dest and branch were confirmed new under the
            # lock moments ago, but clone_lock excludes only cooperating callers in
            # this process: the discard is best-effort and could remove a same-named
            # branch or dest something else (the agent's own git) made in that window.
            log.info("worktree.fork_failed step=add dest=%s branch=%s err=timeout",
                     dest, branch)
            _discard_fork(repo_top, dest, branch)
            raise ForkWorktreeError("git timed out creating the fork worktree") from exc
        if added is None:
            # Not discarded: a failed add may have hit a dest or branch that is not ours.
            log.info("worktree.fork_failed step=add dest=%s branch=%s", dest, branch)
            raise ForkWorktreeError("git worktree add failed for the fork")
        step = "lock"
        try:
            lock_worktree(repo_top, Path(dest))
            step = "verify"
            child_head = run_git(dest, *NO_SUBMODULE_RECURSE, "rev-parse", "--verify", "-q", "HEAD")
            if child_head is None or os.fsdecode(child_head.strip()) != head_sha:
                raise ForkWorktreeError("the fork worktree is not at the source's HEAD")
            if stash_sha:
                step = "apply"
                if run_git(dest, *git, "stash", "apply", "--index", "-q", stash_sha,
                           timeout=FORK_GIT_TIMEOUT_S) is None:
                    raise ForkWorktreeError("the source's changes did not apply cleanly",
                                            FORK_APPLY_FAILED)
            step = "untracked"
            _copy_untracked(top, dest, untracked)
        except BaseException as exc:
            _discard_fork(repo_top, dest, branch)
            log.info("worktree.fork_failed step=%s dest=%s branch=%s err=%s",
                     step, dest, branch, exc)
            if isinstance(exc, ForkWorktreeError):
                raise
            if isinstance(exc, GitTimeout):
                raise ForkWorktreeError(f"git timed out during fork ({step})") from exc
            if isinstance(exc, Exception):
                raise ForkWorktreeError(f"fork failed ({step}): {exc}") from exc
            raise
    cwd = os.path.join(dest, rel) if rel != "." else dest
    if not os.path.isdir(cwd):  # the source sat in an untracked-only or ignored dir
        cwd = dest
    log.info("worktree.fork_create source=%s head=%s stash=%s untracked=%d branch=%s path=%s",
             top, head_sha, stash_sha or "-", len(untracked), branch, dest)
    return ForkWorktreeInfo(path=dest, cwd=cwd, branch=branch, repo=repo_top)


# ---------------------------------------------------------------------------
# Recovery refs (#2913/#2914): a snapshot taken before anything destructive.
# ---------------------------------------------------------------------------

#: Namespace for recovery snapshots. Lives in the clone's COMMON git dir, so a ref
#: outlives the worktree it was taken from. Layout:
#: ``<ns>/<short-session-id>/<utc-ts>-<reason>``.
RECOVERY_REF_NS = f"refs/{brand.SLUG}/recovery"
#: Why a ref was written — encoded in its name. Only ``drain`` refs are routine
#: (every deploy writes one), so only they are pruned; the others mark a single
#: destructive event and may be the only copy of lost work, so they are kept.
RECOVERY_DRAIN = "drain"
RECOVERY_DELETE = "delete"
RECOVERY_FORCED = "forced"
RECOVERY_BRANCH = "branch"
#: A person's Discard (D-19): the session ends and its worktree is removed.
RECOVERY_DISCARD = "discard"
RECOVERY_REASONS: frozenset[str] = frozenset(
    {RECOVERY_DRAIN, RECOVERY_DELETE, RECOVERY_FORCED, RECOVERY_BRANCH, RECOVERY_DISCARD})
#: Newest ``drain`` refs kept per session.
RECOVERY_KEEP = 5
#: Per-git-call ceiling inside a snapshot. A huge untracked tree must not hang a
#: drain or a delete; the snapshot is then skipped (logged), never raised.
_RECOVERY_GIT_TIMEOUT_S = 60.0
_REF_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")


def _recovery_ns(session_id: str) -> str:
    """Keyed on the SHORT id — the name the session's worktree dir carries — so a
    caller that only knows the directory (the janitor) writes where every reader
    looks, whether it was handed the full id or the short one."""
    part = _REF_SAFE_RE.sub("_", _short(session_id)).strip(".") or "unknown"
    return f"{RECOVERY_REF_NS}/{part}"


def restore_command(ref: str) -> str:
    """The manual restore a human runs inside the worktree (spec rule 4). The ref is
    shell-quoted: the line is copied and pasted into a shell."""
    return f"git restore --source={shlex.quote(ref)} --worktree --staged -- . && git reset"


def _git_timed(args: list[str], env: dict[str, str] | None = None
               ) -> subprocess.CompletedProcess[str]:
    """``_run`` with a timeout — raises ``subprocess.TimeoutExpired``."""
    return subprocess.run(args, capture_output=True, text=True, env=env,
                          timeout=_RECOVERY_GIT_TIMEOUT_S)


#: The ``<utc-ts>`` half of a recovery ref's leaf name (``_write_recovery_ref``).
_RECOVERY_STAMP_FMT = "%Y%m%dT%H%M%S%fZ"
#: A leaf ``_write_recovery_ref`` writes: ``<_RECOVERY_STAMP_FMT>-<reason>`` (strftime
#: pads ``%f`` to six digits). Anything else under the namespace was not written by us.
_RECOVERY_LEAF_RE = re.compile(
    r"(\d{8}T\d{12}Z)-(" + "|".join(sorted(RECOVERY_REASONS)) + r")")
#: Characters a listed ref may carry. Git allows ``;``, ``$``, backticks, ``|`` … in a
#: refname, and a listed ref is pasted into a shell (``restore_command``).
_RECOVERY_REFNAME_RE = re.compile(r"[A-Za-z0-9._/-]+")
_OBJECT_ID_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


@dataclass(frozen=True)
class RecoveryRef:
    """One recovery ref as its (well-formed) name and object encode it."""
    ref: str
    sha: str
    reason: str
    created_at: datetime


def _parse_recovery_ref(ns: str, ref: str, sha: str) -> RecoveryRef | None:
    """The entry for a ref exactly ``<ns>/<utc-ts>-<reason>`` of safe characters
    pointing at an object id, else None — never listed, never a restore source."""
    if not _RECOVERY_REFNAME_RE.fullmatch(ref) or not _OBJECT_ID_RE.fullmatch(sha):
        return None
    head, _, leaf = ref.rpartition("/")
    m = _RECOVERY_LEAF_RE.fullmatch(leaf)
    if head != ns or m is None:
        return None
    try:
        created = datetime.strptime(m.group(1), _RECOVERY_STAMP_FMT).replace(
            tzinfo=timezone.utc)
    except ValueError:
        return None
    return RecoveryRef(ref=ref, sha=sha, reason=m.group(2), created_at=created)


def recovery_ref_entries(repo_path: str, session_id: str) -> list[RecoveryRef] | None:
    """Recovery refs recorded for *session_id* in *repo_path*'s clone (or any of its
    worktrees — the refs live in the common git dir), newest first, with each ref's
    sha and what its name encodes. Only well-formed names are listed (see
    ``_parse_recovery_ref``): a ref someone planted under the namespace is skipped.
    One ``for-each-ref``; None when git cannot list (not a repo, dangling gitdir)."""
    ns = _recovery_ns(session_id)
    r = _run(["git", "-C", repo_path, "for-each-ref", "--sort=-refname",
              "--format=%(refname) %(objectname)", f"{ns}/"])
    if r.returncode != 0:
        return None
    out: list[RecoveryRef] = []
    for ln in r.stdout.splitlines():
        ref, _, sha = ln.strip().partition(" ")
        entry = _parse_recovery_ref(ns, ref, sha.strip())
        if entry is not None:
            out.append(entry)
    return out


def recovery_refs(repo_path: str, session_id: str) -> list[str]:
    """Recovery refs recorded for *session_id* in *repo_path*'s clone, newest first
    (well-formed names only; [] when git cannot list)."""
    return [e.ref for e in recovery_ref_entries(repo_path, session_id) or []]


def _kept_ref_matching(repo_path: str, session_id: str, rev_suffix: str,
                       value: str) -> str | None:
    """An existing recovery ref whose ``<ref><rev_suffix>`` resolves to *value* —
    i.e. what we are about to keep is already kept. The newest ref of any reason
    counts; an older one only when it is never pruned (not ``drain``), so a match can
    never rotate out from under the caller. Checking past the newest is what keeps a
    Discard's own refs (dirty tree, then branch tip) from being written a second time
    by the ``remove_worktree`` that follows it."""
    for i, ref in enumerate(recovery_refs(repo_path, session_id)):
        if i and ref.endswith(f"-{RECOVERY_DRAIN}"):
            continue
        r = _run(["git", "-C", repo_path, "rev-parse", "--verify", "--quiet",
                  f"{ref}{rev_suffix}"])
        if r.returncode == 0 and r.stdout.strip() == value:
            return ref
    return None


def _write_recovery_ref(repo_path: str, session_id: str, commit: str,
                        reason: str) -> str | None:
    """Point a fresh ``<ts>-<reason>`` recovery ref at *commit*; prune drain refs."""
    stamp = datetime.now(timezone.utc).strftime(_RECOVERY_STAMP_FMT)
    ref = f"{_recovery_ns(session_id)}/{stamp}-{reason}"
    r = _git_timed(["git", "-C", repo_path, "update-ref", ref, commit])
    if r.returncode != 0:
        log.warning("worktree.recovery_ref_failed session=%s err=%s",
                    session_id, r.stderr.strip())
        return None
    drains = [x for x in recovery_refs(repo_path, session_id)
              if x.endswith(f"-{RECOVERY_DRAIN}")]
    for stale in drains[RECOVERY_KEEP:]:
        _run(["git", "-C", repo_path, "update-ref", "-d", stale])
    log.info("worktree.recovery_ref session=%s ref=%s restore=%r",
             session_id, ref, restore_command(ref))
    return ref


def snapshot_recovery_ref(worktree: str, session_id: str, *, reason: str) -> str | None:
    """Record the worktree's FULL state (staged, unstaged, untracked) as a commit
    under ``refs/viberpit/recovery/<short-sid>/<utc-ts>-<reason>`` without touching
    the user's index or files: ``git add -A`` runs against a temp copy of the index.

    *reason* is one of ``drain`` / ``delete`` / ``forced`` / ``discard`` (it names the ref and
    decides pruning). Returns the ref; the newest existing ref when it already holds
    this exact tree (nothing new is written, so repeated drains never rotate the one
    copy out); or None when the tree is clean (equals ``HEAD^{tree}``) or the
    snapshot could not be taken. Never raises — every caller is about to do
    something destructive (or is draining) and must not be blocked by a failed
    safety net; a failure is logged instead.
    """
    import tempfile
    if reason not in RECOVERY_REASONS - {RECOVERY_BRANCH}:
        log.warning("worktree.recovery_snapshot_failed session=%s bad reason=%r",
                    session_id, reason)
        return None
    try:
        if not os.path.isdir(worktree):
            return None
        idx = _git_timed(["git", "-C", worktree, "rev-parse", "--path-format=absolute",
                          "--git-path", "index"])
        if idx.returncode != 0:
            return None
        head_r = _git_timed(["git", "-C", worktree, "rev-parse", "--verify", "--quiet", "HEAD"])
        head = head_r.stdout.strip() if head_r.returncode == 0 else None
        ident = {"GIT_AUTHOR_NAME": brand.NAME, "GIT_AUTHOR_EMAIL": f"{brand.SLUG}@localhost",
                 "GIT_COMMITTER_NAME": brand.NAME,
                 "GIT_COMMITTER_EMAIL": f"{brand.SLUG}@localhost"}
        with tempfile.TemporaryDirectory(prefix=f"{brand.SLUG}-recovery-") as td:
            tmp_index = os.path.join(td, "index")
            real_index = idx.stdout.strip()
            if os.path.exists(real_index):
                shutil.copy2(real_index, tmp_index)
            env = {**os.environ, "GIT_INDEX_FILE": tmp_index, **ident}
            add = _git_timed(["git", "-C", worktree, "add", "-A"], env=env)
            if add.returncode != 0:
                log.warning("worktree.recovery_snapshot_failed session=%s step=add err=%s",
                            session_id, add.stderr.strip())
                return None
            wt_r = _git_timed(["git", "-C", worktree, "write-tree"], env=env)
            if wt_r.returncode != 0:
                log.warning("worktree.recovery_snapshot_failed session=%s step=write-tree "
                            "err=%s", session_id, wt_r.stderr.strip())
                return None
        tree = wt_r.stdout.strip()
        if head is not None:
            head_tree = _git_timed(["git", "-C", worktree, "rev-parse", f"{head}^{{tree}}"])
            if head_tree.returncode == 0 and head_tree.stdout.strip() == tree:
                return None
        else:
            listed = _git_timed(["git", "-C", worktree, "ls-tree", tree])
            if listed.returncode == 0 and not listed.stdout.strip():
                return None
        same = _kept_ref_matching(worktree, session_id, "^{tree}", tree)
        if same is not None:
            return same
        cmd = ["git", "-C", worktree, "commit-tree", tree]
        if head is not None:
            cmd += ["-p", head]
        cmd += ["-m", f"{brand.SLUG} recovery: {reason}"]
        commit = _git_timed(cmd, env={**os.environ, **ident})
        if commit.returncode != 0:
            log.warning("worktree.recovery_snapshot_failed session=%s step=commit-tree "
                        "err=%s", session_id, commit.stderr.strip())
            return None
        return _write_recovery_ref(worktree, session_id, commit.stdout.strip(), reason)
    except Exception:
        log.warning("worktree.recovery_snapshot_failed session=%s path=%s",
                    session_id, worktree, exc_info=True)
        return None


def branch_presence(repo_path: str, branch: str) -> bool | None:
    """Whether ``refs/heads/<branch>`` exists in *repo_path*'s clone: True, False, or
    None when git could not say. Only ``rev-parse --verify --quiet``'s exit 1 with no
    stderr is proof of absence — a missing repo, a lock or any other failure is
    unknown, never "absent" (a caller that forgets or reports a deletion on False
    must not do it on a git error)."""
    if not branch:
        return False
    r = _run(["git", "-C", repo_path, "rev-parse", "--verify", "--quiet",
              f"refs/heads/{branch}"])
    if r.returncode == 0:
        return True
    if r.returncode == 1 and not r.stderr.strip():
        return False
    return None


def local_branch_exists(repo_path: str, branch: str) -> bool:
    """Whether ``refs/heads/<branch>`` is known to exist in *repo_path*'s clone. False
    means absent OR unknown — use :func:`branch_presence` when the difference matters."""
    return branch_presence(repo_path, branch) is True


def _branch_has_unpushed_commits(repo_top: str, branch: str) -> bool | None:
    """True when *branch* holds commits reachable from no remote-tracking ref and no
    OTHER local branch — work that ``git branch -D`` would leave unreferenced. None
    when git cannot say: every caller must treat that as "at risk, cannot be kept"
    and keep the branch (it used to read as False, and the branch was deleted)."""
    r = _run(["git", "-C", repo_top, "rev-list", "--count", f"refs/heads/{branch}",
              "--not", "--remotes", f"--exclude={branch}", "--branches"])
    if r.returncode != 0:
        return None
    try:
        return int(r.stdout.strip() or "0") > 0
    except ValueError:
        return None


class RecoveryUnknown(Exception):
    """Whether a removal would lose commits could not be determined (git failed), so
    nothing may be removed on the assumption that it would not."""


def snapshot_branch_tip(repo_top: str, branch: str, session_id: str) -> str | None:
    """Before a session branch is deleted, keep its tip as
    ``refs/viberpit/recovery/<short-sid>/<ts>-branch`` iff it has unpushed commits.
    None also when that cannot be determined — callers that delete the branch check
    ``_branch_has_unpushed_commits`` themselves first. Never raises."""
    try:
        if not _branch_has_unpushed_commits(repo_top, branch):
            return None
        tip = _run(["git", "-C", repo_top, "rev-parse", "--verify", "--quiet",
                    f"refs/heads/{branch}"])
        if tip.returncode != 0:
            return None
        sha = tip.stdout.strip()
        same = _kept_ref_matching(repo_top, session_id, "", sha)
        if same is not None:
            return same
        return _write_recovery_ref(repo_top, session_id, sha, RECOVERY_BRANCH)
    except Exception:
        log.warning("worktree.recovery_branch_failed session=%s branch=%s",
                    session_id, branch, exc_info=True)
        return None


def snapshot_before_discard(worktree: str, session_id: str, *,
                            owned_branch: Callable[[str], bool]) -> list[str] | None:
    """Keep everything a Discard (D-19) would destroy, BEFORE anything is touched.

    A Discard force-removes the worktree and ``branch -D``'s its session branch
    (``remove_worktree``), so three things can be lost, and each gets a recovery ref:

    * a dirty tree (staged, unstaged, untracked) — ``snapshot_recovery_ref`` with
      reason ``discard``;
    * a session branch's commits reachable from no remote and no other branch —
      ``snapshot_branch_tip``;
    * a DETACHED HEAD's commits on no branch and no remote — the HEAD commit, reason
      ``discard``.

    A user's own branch is never deleted, so its commits are not at risk. Returns the
    refs now holding the work (dirty tree first), ``[]`` when nothing would be lost,
    or None when something WOULD be lost and could not be kept, or the tree could not
    be read — the caller must then refuse the discard. Raises ``RecoveryUnknown`` when
    git cannot say whether commits would be lost (also a refusal). Fails closed.
    """
    try:
        if not os.path.isdir(worktree):
            return None
        status = _git_timed(["git", "-C", worktree, "status", "--porcelain"])
        common = _git_timed(["git", "-C", worktree, "rev-parse", "--path-format=absolute",
                             "--git-common-dir"])
        head_r = _git_timed(["git", "-C", worktree, "rev-parse", "--verify", "--quiet", "HEAD"])
        if status.returncode != 0 or common.returncode != 0 or head_r.returncode != 0:
            return None
        repo_top = os.path.dirname(common.stdout.strip().rstrip("/"))
        head = head_r.stdout.strip()
        refs: list[str] = []
        if status.stdout.strip():
            ref = snapshot_recovery_ref(worktree, session_id, reason=RECOVERY_DISCARD)
            if ref is None:
                return None
            refs.append(ref)
        branch = current_branch(worktree)
        # The SAME ownership predicate the removal will ask (D-22): a branch it would
        # delete — custom prefix included — gets its tip kept first; a branch it keeps
        # loses nothing. A predicate that raises owns nothing, as in remove_worktree.
        try:
            owned = branch is not None and bool(owned_branch(branch))
        except Exception:
            owned = False
        if branch is not None and owned:
            at_risk = _branch_has_unpushed_commits(repo_top, branch)
            if at_risk is None:
                raise RecoveryUnknown(f"unpushed commits on {branch} unknown")
            if at_risk:
                ref = snapshot_branch_tip(repo_top, branch, session_id)
                if ref is None:
                    return None
                refs.append(ref)
        elif branch is None:
            orphan = _git_timed(["git", "-C", repo_top, "rev-list", "--count", head,
                                 "--not", "--remotes", "--branches"])
            if orphan.returncode != 0:
                raise RecoveryUnknown("commits on the detached HEAD unknown")
            if int(orphan.stdout.strip() or "0") > 0:
                ref = (_kept_ref_matching(repo_top, session_id, "", head)
                       or _write_recovery_ref(repo_top, session_id, head, RECOVERY_DISCARD))
                if ref is None:
                    return None
                refs.append(ref)
        return list(dict.fromkeys(refs))
    except RecoveryUnknown:
        raise
    except Exception as exc:
        # INFO with structured fields: the caller's refusal (the orchestrator's
        # `session.worktree_discard_snapshot_failed`) is the incident's one WARNING.
        _log_fields(logging.INFO, "worktree.discard_snapshot_failed", session=session_id,
                    path=worktree, err=repr(exc))
        return None


def recreate_worktree(repo_path: str, path: str, branch: str, *,
                      person_initiated: bool = False) -> bool:
    """Re-add a missing (or EMPTY) managed worktree at *path* from its LOCAL *branch*.

    An empty directory is the leftover of the old unconditional ``mkdir`` (the
    literal #2913 state) and holds nothing to lose, so it is recreated in place.
    Returns False — touching nothing at *path* — when the path exists and is not an
    empty directory (a present tree is never recreated, spec rule 1), the branch is
    not in the local clone, or ``git worktree add`` refuses. Clears only *path*'s
    own stale admin record first (``clear_stale_registration``), never a
    repo-global prune.
    """
    if not branch or branch.startswith("-"):
        return False
    dest = Path(path)
    if dest.exists() and not (dest.is_dir() and not any(dest.iterdir())):
        return False
    top = _run(["git", "-C", repo_path, "rev-parse", "--show-toplevel"])
    if top.returncode != 0:
        return False
    repo_top = top.stdout.strip()
    if _run(["git", "-C", repo_top, "rev-parse", "--verify", "--quiet",
             f"refs/heads/{branch}"]).returncode != 0:
        return False
    with clone_lock(repo_top):
        if dest.exists():
            try:
                os.rmdir(dest)   # raises rather than remove anything non-empty
            except OSError:
                return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        clear_stale_registration(repo_top, dest)
        r = _run(["git", "-C", repo_top, "worktree", "add", str(dest), "--", branch])
        if r.returncode != 0:
            # A person's Repair (D-19) gets the refusal back as a code: INFO, not an
            # auto-filed WARNING. Fields are structured, so the path never splits the
            # fingerprint.
            _log_fields(logging.INFO if person_initiated else logging.WARNING,
                        "worktree.recreate_failed", path=str(dest), branch=branch,
                        err=r.stderr.strip())
            return False
        lock_worktree(repo_top, dest)
    return True


def remove_worktree(worktree_path: str, force: bool = False, *,
                    session_id: str | None = None,
                    owned_branch: Callable[[str], bool]) -> bool:
    """Remove a ViberPit worktree IFF it has no uncommitted changes.

    Returns True if removed, False if it was left in place (dirty — we never
    destroy uncommitted work). Also deletes the branch it had checked out, but only
    when *owned_branch* says ViberPit created it (D-22) — a name match is never proof.
    *owned_branch* is REQUIRED, with no default, so no caller can skip the question:
    the server builds it from the branch ownership records
    (``server/sessions/branch_ownership.py``); a predicate that raises keeps the branch.

    ``force=True`` bypasses the dirty guard and uses ``git worktree remove
    --force``.  Only the janitor's max-age eviction path should set this —
    normal teardown must never discard uncommitted work.

    Before anything is discarded a recovery ref is written (#2913): a forced
    removal of a dirty tree snapshots it, and a session branch about to be
    ``branch -D``'d keeps its tip when it holds unpushed commits. *session_id*
    names the refs; it defaults to the worktree's directory name (the session's
    short id, which is how managed worktrees are named). Both fail closed: a dirty tree
    (or one whose ``git status`` fails, which counts as dirty)
    whose snapshot fails is not removed (False), and a branch whose unpushed state
    is unknown, or whose tip could not be kept, is not deleted.

    Every ``False`` leaves a journal line naming why.  The two pre-flight
    refusals below used to return silently, which is what made the janitor's
    stuck-eviction loop undiagnosable: 916 sweeps refused the same eleven
    worktrees without a single line saying what git objected to (#1664).
    """
    wt = os.path.abspath(worktree_path)
    if not os.path.isdir(wt):
        log.info("worktree.remove_skipped path=%s reason=not_a_directory", wt)
        return False

    # A Codex session may have staged attachment copies under the workspace
    # (server/codex/driver.py:_stage_in_workspace). That dir is registered in
    # `.git/info/exclude` on first use, but the write is best-effort and older
    # worktrees predate the feature — remove it here too, BEFORE the dirty
    # check below, so a leftover staging dir never falsely marks an otherwise
    # untouched worktree dirty and blocks its own removal.
    for dirname in brand.CODEX_ATTACHMENTS_DIRNAMES:
        staging = os.path.join(wt, dirname)
        if os.path.isdir(staging):
            shutil.rmtree(staging, ignore_errors=True)

    # Resolve the main repo this worktree belongs to (its common git dir is
    # <repo>/.git; the worktree's own .git is just a pointer file).
    common = _run(["git", "-C", wt, "rev-parse", "--path-format=absolute", "--git-common-dir"])
    if common.returncode != 0:
        log.info(
            "worktree.remove_skipped path=%s reason=no_git_common_dir err=%s",
            wt, common.stderr.strip(),
        )
        return False
    repo_top = os.path.dirname(common.stdout.strip().rstrip("/"))

    # Never blow away uncommitted work (unless the caller explicitly forces eviction).
    # A status that failed is not "clean" (D-19 final review): it used to read as one,
    # so a forced removal skipped its snapshot. It counts as dirty — an unforced caller
    # keeps the tree, a forced one removes it only once the snapshot below exists.
    status = _run(["git", "-C", wt, "status", "--porcelain"])
    unreadable = status.returncode != 0
    is_dirty = unreadable or bool(status.stdout.strip())
    if is_dirty and not force:
        log.info("worktree.keep_dirty path=%s%s", wt,
                 " reason=status_failed" if unreadable else "")
        return False
    sid = session_id or os.path.basename(wt.rstrip("/"))
    if is_dirty and snapshot_recovery_ref(wt, sid, reason=RECOVERY_FORCED) is None:
        # No copy of the dirty tree exists: a forced removal would destroy the only one.
        log.info("worktree.keep_dirty path=%s reason=snapshot_failed", wt)
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

    # Tidy up the branch ViberPit created for this session — only when the caller's
    # ownership predicate (recorded creation, or the session's own legacy shape) says
    # so; a user's own branch, or any branch nobody can vouch for, is kept (D-22).
    # `remove` already deregisters this worktree; no global prune (it could reap a
    # sibling session's live worktree — #177).
    # The branch is deleted only when nothing on it would be lost: no unpushed commits,
    # or their tip is kept as a recovery ref. Unknown (git failed) or a failed snapshot
    # keeps the branch — it used to be deleted either way.
    try:
        owned = bool(branch) and branch != "HEAD" and bool(owned_branch(branch))
    except Exception:
        _log_fields(logging.WARNING, "worktree.keep_branch", path=wt, branch=branch,
                    reason="ownership_unknown")
        owned = False
    if not owned and branch and branch != "HEAD":
        log.info("worktree.keep_branch path=%s branch=%s reason=not_owned", wt, branch)
    if owned:
        at_risk = _branch_has_unpushed_commits(repo_top, branch)
        if at_risk is None:
            log.info("worktree.keep_branch path=%s branch=%s reason=unpushed_unknown",
                     wt, branch)
        elif at_risk and snapshot_branch_tip(repo_top, branch, sid) is None:
            _log_fields(logging.WARNING, "worktree.keep_branch", path=wt, branch=branch,
                        reason="snapshot_failed")
        else:
            _run(["git", "-C", repo_top, "branch", "-D", branch])
    log.info("worktree.remove path=%s branch=%s force=%s", wt, branch, force)
    return True
