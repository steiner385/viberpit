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
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


class WorktreeError(Exception):
    """Raised when a worktree could not be created."""


@dataclass
class WorktreeInfo:
    path: str             # the worktree directory — use this as the session cwd
    branch: str | None    # the branch checked out, or None for a detached worktree
    is_child: bool        # True if we created a child branch (selected branch was busy)
    repo: str             # absolute toplevel of the main repo the worktree belongs to


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True)


def _has_origin(repo_top: str) -> bool:
    """True if the repo has an `origin` remote configured."""
    return _run(["git", "-C", repo_top, "remote", "get-url", "origin"]).returncode == 0


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

    A fetch failure is best-effort, NOT fatal: many real origins are not fetchable
    non-interactively (private repos with no cached credentials, the credential-less
    deploy box, offline). On failure we log a warning and return False so the caller
    bases the session on the local ref rather than refusing to start it.
    """
    r = _run(["git", "-C", repo_top, "fetch", "origin"])
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
        if b and b not in skip and not _SHA_RE.match(b)
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


def create_worktree(repo_path: str, branch: str | None, session_id: str, repo_root: str) -> WorktreeInfo:
    """Create an isolated per-session worktree and return its info.

    When `branch` is given, checks it out directly; if it is already checked out
    elsewhere, creates a child branch ``ccswitch/<branch>-<short>`` instead. When
    `branch` is None, creates a DETACHED worktree off the repo's current HEAD — used
    for repo sessions that didn't pick a branch, so every session still gets its own
    isolated working directory (never sharing the clone) without needing a branch.

    Raises:
        WorktreeError: if `repo_path` is not a git repo, or worktree creation fails.
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

    # Drop admin records for any worktree dirs that were deleted out from under git
    # (e.g. a previous crash) so a reused path doesn't collide.
    _run(["git", "-C", repo_top, "worktree", "prune"])

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
        log.info("worktree.create repo=%s branch=%s path=%s", repo_name, branch, dest)
        return WorktreeInfo(str(dest), branch, is_child=False, repo=repo_top)

    # 3) Branch is busy (checked out elsewhere) — make a session child branch off it.
    child = f"ccswitch/{branch}-{_short(session_id)}"
    r2 = _run(["git", "-C", repo_top, "worktree", "add", "-b", child, str(dest), "--", branch])
    if r2.returncode == 0:
        log.info("worktree.create_child repo=%s base=%s child=%s path=%s",
                 repo_name, branch, child, dest)
        return WorktreeInfo(str(dest), child, is_child=True, repo=repo_top)

    raise WorktreeError(
        f"git worktree add failed for {branch!r}: "
        f"{r.stderr.strip()} / {r2.stderr.strip()}"
    )


def remove_worktree(worktree_path: str) -> bool:
    """Remove a ccswitch worktree IFF it has no uncommitted changes.

    Returns True if removed, False if it was left in place (dirty — we never
    destroy uncommitted work). Also deletes the per-session ``ccswitch/*`` child
    branch when one was created, so the repo stays tidy.
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

    # Never blow away uncommitted work.
    status = _run(["git", "-C", wt, "status", "--porcelain"])
    if status.returncode == 0 and status.stdout.strip():
        log.info("worktree.keep_dirty path=%s", wt)
        return False

    branch_r = _run(["git", "-C", wt, "rev-parse", "--abbrev-ref", "HEAD"])
    branch = branch_r.stdout.strip() if branch_r.returncode == 0 else ""

    rm = _run(["git", "-C", repo_top, "worktree", "remove", wt])
    if rm.returncode != 0:
        log.warning("worktree.remove_failed path=%s err=%s", wt, rm.stderr.strip())
        return False

    # Tidy up the session child branch we created (only the ccswitch/* ones).
    if branch.startswith("ccswitch/"):
        _run(["git", "-C", repo_top, "branch", "-D", branch])
    _run(["git", "-C", repo_top, "worktree", "prune"])
    log.info("worktree.remove path=%s branch=%s", wt, branch)
    return True
