"""Per-session git worktrees so concurrent sessions on one repo don't collide.

Each ViberPit session that selects a repo + branch runs in its OWN git worktree
checked out to that branch, instead of mutating the user's real checkout with a
`git checkout`.  This means:

  * The user's real `~/GitHub/<repo>` working tree is never touched (its HEAD,
    branch, and uncommitted files stay exactly as they were).
  * Two sessions on the same repo can be on different branches at once — each has
    a separate working tree, so there is no collision.

Worktrees live under ``<repo_root>/<namespace>/worktrees/<repo>/<short-session>``
(a ViberPit-owned namespace next to the user's repos — ``paths.repo_namespace``:
the brand dot-dir, or the legacy one on a root that only has that), and share the original
repo's object store (cheap — no full clone).

If the selected branch is already checked out somewhere (commonly the user's real
checkout sitting on `main`), git refuses to check it out twice, so we fall back to
a per-session child branch ``viberpit/<branch>-<short>`` based on it — still
isolated, still writable, and clearly named.  Branches created before the rebrand
carry the ``ccswitch/`` prefix and are matched too (``brand.is_session_branch``);
that accept side is permanent, because a pushed branch outlives the flip.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from . import brand, gh_credential, paths
from .gitrepos import STALE_LOCK_AGE_S, clone_lock, git_env_for_remote

log = logging.getLogger(__name__)

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
    """ViberPit-owned root NEW session worktrees are created under (see
    ``paths.repo_namespace`` for which dot-dir that is)."""
    return paths.repo_namespace_dir(repo_root) / "worktrees"


def worktrees_bases(repo_root: str) -> tuple[Path, ...]:
    """Every worktrees root a reader must scan — both namespace spellings (#1373)."""
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


def _child_branch(base: str, session_id: str) -> str:
    """Name of the per-session child branch created off *base*.

    One function rather than the two identical f-strings create_worktree used to
    carry: the name it emits must stay inside the set ``brand.is_session_branch``
    recognises, and remove_worktree's ``git branch -D`` is what enforces that.
    """
    return f"{brand.BRANCH_PREFIX}{base}-{_short(session_id)}"


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


def create_worktree(repo_path: str, branch: str | None, session_id: str, repo_root: str) -> WorktreeInfo:
    """Create an isolated per-session worktree and return its info.

    When `branch` is given, checks it out directly; if it is already checked out
    elsewhere, creates a child branch ``viberpit/<branch>-<short>`` instead. When
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
            child = _child_branch(branch, session_id)
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
        child = _child_branch(branch, session_id)
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
RECOVERY_REASONS: frozenset[str] = frozenset(
    {RECOVERY_DRAIN, RECOVERY_DELETE, RECOVERY_FORCED, RECOVERY_BRANCH})
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
    """The manual restore a human runs inside the worktree (spec rule 4)."""
    return f"git restore --source={ref} --worktree --staged -- . && git reset"


def _git_timed(args: list[str], env: dict[str, str] | None = None
               ) -> subprocess.CompletedProcess[str]:
    """``_run`` with a timeout — raises ``subprocess.TimeoutExpired``."""
    return subprocess.run(args, capture_output=True, text=True, env=env,
                          timeout=_RECOVERY_GIT_TIMEOUT_S)


def recovery_refs(repo_path: str, session_id: str) -> list[str]:
    """Recovery refs recorded for *session_id* in *repo_path*'s clone, newest first."""
    r = _run(["git", "-C", repo_path, "for-each-ref", "--sort=-refname",
              "--format=%(refname)", f"{_recovery_ns(session_id)}/"])
    if r.returncode != 0:
        return []
    return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]


def _newest_ref_matching(repo_path: str, session_id: str, rev_suffix: str,
                         value: str) -> str | None:
    """The newest existing recovery ref iff ``<ref><rev_suffix>`` resolves to
    *value* — i.e. what we are about to keep is already kept."""
    refs = recovery_refs(repo_path, session_id)
    if not refs:
        return None
    r = _run(["git", "-C", repo_path, "rev-parse", "--verify", "--quiet",
              f"{refs[0]}{rev_suffix}"])
    return refs[0] if r.returncode == 0 and r.stdout.strip() == value else None


def _write_recovery_ref(repo_path: str, session_id: str, commit: str,
                        reason: str) -> str | None:
    """Point a fresh ``<ts>-<reason>`` recovery ref at *commit*; prune drain refs."""
    from datetime import datetime, timezone
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
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

    *reason* is one of ``drain`` / ``delete`` / ``forced`` (it names the ref and
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
        same = _newest_ref_matching(worktree, session_id, "^{tree}", tree)
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


def _branch_has_unpushed_commits(repo_top: str, branch: str) -> bool:
    """True when *branch* holds commits reachable from no remote-tracking ref and no
    OTHER local branch — work that ``git branch -D`` would leave unreferenced."""
    r = _run(["git", "-C", repo_top, "rev-list", "--count", f"refs/heads/{branch}",
              "--not", "--remotes", f"--exclude={branch}", "--branches"])
    if r.returncode != 0:
        return False
    try:
        return int(r.stdout.strip() or "0") > 0
    except ValueError:
        return False


def snapshot_branch_tip(repo_top: str, branch: str, session_id: str) -> str | None:
    """Before a session branch is deleted, keep its tip as
    ``refs/viberpit/recovery/<short-sid>/<ts>-branch`` iff it has unpushed commits.
    Never raises."""
    try:
        if not _branch_has_unpushed_commits(repo_top, branch):
            return None
        tip = _run(["git", "-C", repo_top, "rev-parse", "--verify", "--quiet",
                    f"refs/heads/{branch}"])
        if tip.returncode != 0:
            return None
        sha = tip.stdout.strip()
        same = _newest_ref_matching(repo_top, session_id, "", sha)
        if same is not None:
            return same
        return _write_recovery_ref(repo_top, session_id, sha, RECOVERY_BRANCH)
    except Exception:
        log.warning("worktree.recovery_branch_failed session=%s branch=%s",
                    session_id, branch, exc_info=True)
        return None


def recreate_worktree(repo_path: str, path: str, branch: str) -> bool:
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
            log.warning("worktree.recreate_failed path=%s branch=%s err=%s",
                        dest, branch, r.stderr.strip())
            return False
        lock_worktree(repo_top, dest)
    return True


def remove_worktree(worktree_path: str, force: bool = False, *,
                    session_id: str | None = None) -> bool:
    """Remove a ViberPit worktree IFF it has no uncommitted changes.

    Returns True if removed, False if it was left in place (dirty — we never
    destroy uncommitted work). Also deletes the per-session child branch when one
    was created (either brand prefix), so the repo stays tidy.

    ``force=True`` bypasses the dirty guard and uses ``git worktree remove
    --force``.  Only the janitor's max-age eviction path should set this —
    normal teardown must never discard uncommitted work.

    Before anything is discarded a recovery ref is written (#2913): a forced
    removal of a dirty tree snapshots it, and a session branch about to be
    ``branch -D``'d keeps its tip when it holds unpushed commits. *session_id*
    names the refs; it defaults to the worktree's directory name (the session's
    short id, which is how managed worktrees are named).

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
    status = _run(["git", "-C", wt, "status", "--porcelain"])
    is_dirty = status.returncode == 0 and bool(status.stdout.strip())
    if is_dirty and not force:
        log.info("worktree.keep_dirty path=%s", wt)
        return False
    sid = session_id or os.path.basename(wt.rstrip("/"))
    if is_dirty:
        snapshot_recovery_ref(wt, sid, reason=RECOVERY_FORCED)

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

    # Tidy up the session child branch we created (only our own prefixes — a user's
    # own branch is never deleted). BOTH spellings: a worktree created before the
    # rebrand is torn down by the post-rebrand code, so matching only the emitted
    # prefix would leak every legacy child branch into the clone permanently.
    # `remove` already deregisters this worktree; no global prune (it could reap a
    # sibling session's live worktree — #177).
    if brand.is_session_branch(branch):
        snapshot_branch_tip(repo_top, branch, sid)
        _run(["git", "-C", repo_top, "branch", "-D", branch])
    log.info("worktree.remove path=%s branch=%s force=%s", wt, branch, force)
    return True
