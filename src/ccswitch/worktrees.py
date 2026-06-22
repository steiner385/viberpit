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


def _remote_default_ref(repo_top: str) -> str:
    """Return the remote default branch ref (e.g. 'origin/master').

    Repairs origin/HEAD with `set-head --auto` first in case the clone never
    recorded it. Raises WorktreeError if it cannot be resolved.
    """
    _run(["git", "-C", repo_top, "remote", "set-head", "origin", "--auto"])
    r = _run(["git", "-C", repo_top, "symbolic-ref", "--short", "refs/remotes/origin/HEAD"])
    if r.returncode != 0 or not r.stdout.strip():
        raise WorktreeError("could not resolve origin/HEAD (remote default branch)")
    return r.stdout.strip()


def _fetch_origin(repo_top: str) -> None:
    """Fetch `origin`; raise WorktreeError if it fails.

    A configured-but-unreachable origin is fatal: we refuse to silently base a
    session on a stale local ref.
    """
    r = _run(["git", "-C", repo_top, "fetch", "origin"])
    if r.returncode != 0:
        raise WorktreeError(f"git fetch origin failed: {r.stderr.strip()}")


def is_git_repo(path: str) -> bool:
    """True if `path` is inside a git work tree."""
    r = _run(["git", "-C", path, "rev-parse", "--is-inside-work-tree"])
    return r.returncode == 0 and r.stdout.strip() == "true"


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

    # Fetch-first: a session must start from the true remote tip. If origin is
    # configured we fetch and (below) base on remote refs; a fetch failure is
    # fatal — never silently fall back to a stale local ref. A repo with NO
    # origin has nothing to be stale against and keeps the local-ref behavior.
    has_origin = _has_origin(repo_top)
    if has_origin:
        _fetch_origin(repo_top)

    # 0) No branch selected → detached worktree. With origin, detach at the remote
    # default tip (origin/HEAD); without origin, off the local current HEAD.
    if branch is None:
        if has_origin:
            base = _remote_default_ref(repo_top)
            rd = _run(["git", "-C", repo_top, "worktree", "add", "--detach",
                       str(dest), "--", base])
        else:
            rd = _run(["git", "-C", repo_top, "worktree", "add", "--detach", str(dest)])
        if rd.returncode == 0:
            log.info("worktree.create_detached repo=%s path=%s", repo_name, dest)
            return WorktreeInfo(str(dest), None, is_child=False, repo=repo_top)
        raise WorktreeError(f"git worktree add --detach failed: {rd.stderr.strip()}")

    # 1) Try to check out the selected branch directly.
    # '--' separates options from the commit-ish so a crafted branch name cannot
    # be parsed as a git option (belt-and-suspenders with the '-' prefix guard above).
    r = _run(["git", "-C", repo_top, "worktree", "add", str(dest), "--", branch])
    if r.returncode == 0:
        log.info("worktree.create repo=%s branch=%s path=%s", repo_name, branch, dest)
        return WorktreeInfo(str(dest), branch, is_child=False, repo=repo_top)

    # 2) Branch is busy (checked out elsewhere) — make a session child branch off it.
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
