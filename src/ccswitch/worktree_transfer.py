"""Carry a session's worktree between fleet hosts through git.

A session's worktree is the one host-local thing a cross-host transfer must move
(spec: docs/superpowers/specs/2026-09-14-cross-host-session-transfer-design.md).
The source pushes HEAD (plus unpushed authored branches) to a hidden namespace on
``origin``, ``refs/viberpit/transfer/<session>/…``; the target fetches it and
re-creates the worktree at the same path. Uncommitted edits ride inside a WIP
commit that the target unpacks with ``git reset --mixed`` so they arrive as
unstaged edits and the branch tip is exactly where the agent left it.

Pure git plumbing: no orchestrator imports, every function is synchronous and
safe to run under ``asyncio.to_thread``.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from ccswitch.gitrepos import git_env_for_remote
from ccswitch.worktrees import (
    WorktreeInfo, _origin_url, _run, _short, _try_fetch_origin, authored_branches,
    clear_stale_registration, current_branch, lock_worktree, remove_worktree,
    worktrees_base,
)

log = logging.getLogger(__name__)

__all__ = ["TransferError", "WorktreeState", "Bundle", "TRANSFER_REF_NS", "head_ref",
           "branch_ref", "inspect", "export", "import_", "finalize", "delete_transfer_refs"]

#: Hidden ref namespace on origin. Not under refs/heads, so it never appears as a
#: branch, never triggers CI or PR automation, and is deleted after import.
TRANSFER_REF_NS = "refs/viberpit/transfer"
WIP_SUBJECT = "viberpit: transfer WIP for session {session_id}; not for merge"
_WIP_IDENTITY = ["-c", "user.name=ViberPit", "-c", "user.email=viberpit@localhost"]


class TransferError(Exception):
    """A step refused or failed. ``code`` is the stable machine-readable reason."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class WorktreeState:
    dirty: bool
    dirty_files: int
    branch: str | None          # None when detached
    head_sha: str
    unpushed: tuple[str, ...]   # authored branches whose tip is not on origin at the same sha


def head_ref(session_id: str) -> str:
    return f"{TRANSFER_REF_NS}/{session_id}/head"


def branch_ref(session_id: str, name: str) -> str:
    return f"{TRANSFER_REF_NS}/{session_id}/branches/{name}"


def _rev(cwd: str, ref: str) -> str | None:
    r = _run(["git", "-C", cwd, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"])
    out = r.stdout.strip()
    return out if r.returncode == 0 and out else None


def _repo_top(cwd: str) -> str:
    """Toplevel of the clone that owns *cwd* (a worktree or the clone itself)."""
    r = _run(["git", "-C", cwd, "rev-parse", "--path-format=absolute", "--git-common-dir"])
    if r.returncode != 0:
        raise TransferError("not_git", f"{cwd} is not inside a git repository")
    return os.path.dirname(r.stdout.strip().rstrip("/"))


def inspect(cwd: str) -> WorktreeState:
    """What a transfer would have to carry from *cwd*. Read-only, no network."""
    head = _rev(cwd, "HEAD")
    if head is None:
        raise TransferError("not_git", f"{cwd} is not a git worktree")
    status = _run(["git", "-C", cwd, "status", "--porcelain"])
    lines = [ln for ln in status.stdout.splitlines() if ln.strip()]
    branch = current_branch(cwd)
    candidates = set(authored_branches(cwd))
    if branch:
        candidates.add(branch)
    unpushed: list[str] = []
    for b in sorted(candidates):
        local = _rev(cwd, f"refs/heads/{b}")
        if local is None:
            continue
        if _rev(cwd, f"refs/remotes/origin/{b}") != local:
            unpushed.append(b)
    return WorktreeState(dirty=bool(lines), dirty_files=len(lines), branch=branch,
                         head_sha=head, unpushed=tuple(unpushed))
