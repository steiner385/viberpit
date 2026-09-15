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

from .gitrepos import git_env_for_remote
from .worktrees import (
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


@dataclass(frozen=True)
class Bundle:
    """What the source pushed and the target must fetch. Crosses hosts as a dict
    inside the transfer manifest, so every field is a JSON scalar."""

    session_id: str
    branch: str | None
    head_sha: str        # the commit the target checks out (the WIP commit when wip_commit)
    base_sha: str        # the agent's real tip; == head_sha unless wip_commit
    wip_commit: bool
    branches: dict[str, str] = field(default_factory=dict)  # other unpushed branches → sha

    def to_dict(self) -> dict:
        return {"sessionId": self.session_id, "branch": self.branch, "headSha": self.head_sha,
                "baseSha": self.base_sha, "wipCommit": self.wip_commit,
                "branches": dict(self.branches)}

    @classmethod
    def from_dict(cls, d: dict) -> "Bundle":
        return cls(session_id=d["sessionId"], branch=d.get("branch"), head_sha=d["headSha"],
                   base_sha=d["baseSha"], wip_commit=bool(d.get("wipCommit", False)),
                   branches=dict(d.get("branches") or {}))


def export(cwd: str, session_id: str, *, carry_dirty: bool) -> Bundle:
    """Push *cwd*'s HEAD and unpushed authored branches to the transfer namespace.

    A dirty tree is refused unless ``carry_dirty`` (the user ticked the consent
    box; the rebalancer never passes True). With consent the edits become one WIP
    commit on top of HEAD, pushed as the transfer head; on a push failure that
    commit is undone (``reset --mixed``) so the worktree is exactly as found.
    """
    top = _repo_top(cwd)
    url = _origin_url(top)
    if not url:
        raise TransferError("no_origin", f"{top} has no origin remote")
    _try_fetch_origin(top)  # best-effort: makes `unpushed` accurate against a fresh origin
    state = inspect(cwd)
    if state.dirty and not carry_dirty:
        raise TransferError("dirty_worktree", f"{state.dirty_files} uncommitted change(s)")
    base_sha = state.head_sha
    head_sha = base_sha
    wip = False
    if state.dirty:
        add = _run(["git", "-C", cwd, "add", "-A"])
        if add.returncode != 0:
            raise TransferError("wip_commit_failed", add.stderr.strip())
        commit = _run(["git", *_WIP_IDENTITY, "-C", cwd, "commit", "-q", "--no-verify",
                       "-m", WIP_SUBJECT.format(session_id=session_id)])
        if commit.returncode != 0:
            _run(["git", "-C", cwd, "reset", "-q", "--mixed", base_sha])
            raise TransferError("wip_commit_failed", commit.stderr.strip())
        head_sha = _rev(cwd, "HEAD") or base_sha
        wip = True
    refspecs = [f"+{head_sha}:{head_ref(session_id)}"]
    branches: dict[str, str] = {}
    for b in state.unpushed:
        if b == state.branch:
            continue
        sha = _rev(cwd, f"refs/heads/{b}")
        if sha is None:
            continue
        branches[b] = sha
        refspecs.append(f"+{sha}:{branch_ref(session_id, b)}")
    r = _run(["git", "-C", cwd, "push", "-q", "--no-verify", "--atomic", "origin", *refspecs],
             env=git_env_for_remote(url))
    if r.returncode != 0:
        if wip:
            _run(["git", "-C", cwd, "reset", "-q", "--mixed", base_sha])
        raise TransferError("push_failed", r.stderr.strip())
    log.info("transfer.export session=%s head=%s wip=%s branches=%d",
             session_id, head_sha[:12], wip, len(branches))
    return Bundle(session_id=session_id, branch=state.branch, head_sha=head_sha,
                  base_sha=base_sha, wip_commit=wip, branches=branches)


def _branch_checked_out_at(repo_top: str, branch: str) -> str | None:
    """Path of the worktree that has refs/heads/<branch> checked out, else None."""
    r = _run(["git", "-C", repo_top, "worktree", "list", "--porcelain"])
    path: str | None = None
    for line in r.stdout.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):].strip()
        elif line.strip() == f"branch refs/heads/{branch}":
            return path
    return None


def import_(bundle: Bundle, repo_path: str, repo_root: str) -> WorktreeInfo:
    """Fetch *bundle*'s transfer refs into the clone at *repo_path* and re-create the
    session worktree at its canonical path under *repo_root*.

    Idempotent for the restore-to-source case: a worktree already at the path whose
    HEAD is the bundle's head or base is reused untouched. A branch already checked
    out elsewhere on this clone is refused rather than renamed — ``create_worktree``'s
    child-branch fallback would silently change the branch the agent remembers.
    """
    top = _repo_top(repo_path)
    repo_name = os.path.basename(top.rstrip("/"))
    dest = worktrees_base(repo_root) / repo_name / _short(bundle.session_id)
    url = _origin_url(top)
    ns = f"{TRANSFER_REF_NS}/{bundle.session_id}"
    r = _run(["git", "-C", top, "fetch", "-q", "origin", f"+{ns}/*:{ns}/*"],
             env=git_env_for_remote(url))
    if r.returncode != 0:
        raise TransferError("fetch_failed", r.stderr.strip())
    if _rev(top, bundle.head_sha) is None:
        raise TransferError("fetch_failed", f"{bundle.head_sha} absent after fetch")

    if dest.is_dir():
        if _rev(str(dest), "HEAD") in (bundle.head_sha, bundle.base_sha):
            log.info("transfer.import reuse path=%s", dest)
            return WorktreeInfo(str(dest), bundle.branch, is_child=False, repo=top)
        raise TransferError("dest_occupied", f"{dest} exists with a different HEAD")

    dest.parent.mkdir(parents=True, exist_ok=True)
    clear_stale_registration(top, dest)
    if bundle.branch is None:
        add = _run(["git", "-C", top, "worktree", "add", "-q", "--detach", str(dest),
                    "--", bundle.head_sha])
        if add.returncode != 0:
            raise TransferError("worktree_add_failed", add.stderr.strip())
    else:
        busy = _branch_checked_out_at(top, bundle.branch)
        if busy:
            raise TransferError("branch_busy_on_target",
                                f"{bundle.branch} is checked out at {busy}")
        prev = _rev(top, f"refs/heads/{bundle.branch}")
        br = _run(["git", "-C", top, "branch", "-f", bundle.branch, bundle.head_sha])
        if br.returncode != 0:
            raise TransferError("branch_failed", br.stderr.strip())
        add = _run(["git", "-C", top, "worktree", "add", "-q", str(dest), "--", bundle.branch])
        if add.returncode != 0:
            if prev is not None:
                _run(["git", "-C", top, "branch", "-f", bundle.branch, prev])
            else:
                _run(["git", "-C", top, "branch", "-D", bundle.branch])
            raise TransferError("worktree_add_failed", add.stderr.strip())
    if bundle.wip_commit:
        rs = _run(["git", "-C", str(dest), "reset", "-q", "--mixed", bundle.base_sha])
        if rs.returncode != 0:
            raise TransferError("wip_unpack_failed", rs.stderr.strip())
    for name, sha in bundle.branches.items():
        if _rev(top, f"refs/heads/{name}") is None:
            _run(["git", "-C", top, "branch", name, sha])
    lock_worktree(top, dest)
    log.info("transfer.import session=%s path=%s branch=%s wip=%s",
             bundle.session_id, dest, bundle.branch, bundle.wip_commit)
    return WorktreeInfo(str(dest), bundle.branch, is_child=False, repo=top)


def finalize(bundle: Bundle, worktree_path: str) -> bool:
    """Source-side cleanup after a successful import elsewhere.

    The WIP commit was a transport artifact: put the branch tip back at the base
    (``reset --soft`` keeps the edits staged so nothing is lost if the removal below
    is refused), then remove the worktree with force — its contents now live on
    the target. Returns True iff the directory was removed. Safe to call twice.
    """
    if not os.path.isdir(worktree_path):
        return False
    if bundle.wip_commit and _rev(worktree_path, "HEAD") == bundle.head_sha:
        _run(["git", "-C", worktree_path, "reset", "-q", "--soft", bundle.base_sha])
    removed = remove_worktree(worktree_path, force=True)
    log.info("transfer.finalize session=%s removed=%s path=%s",
             bundle.session_id, removed, worktree_path)
    return removed


def delete_transfer_refs(repo_path: str, session_id: str) -> None:
    """Drop the session's transfer namespace locally and on origin. Best-effort."""
    top = _repo_top(repo_path)
    ns = f"{TRANSFER_REF_NS}/{session_id}"
    listed = _run(["git", "-C", top, "for-each-ref", "--format=%(refname)", f"{ns}/"])
    for ref in (ln.strip() for ln in listed.stdout.splitlines() if ln.strip()):
        _run(["git", "-C", top, "update-ref", "-d", ref])
    url = _origin_url(top)
    if not url:
        return
    env = git_env_for_remote(url)
    remote = _run(["git", "-C", top, "ls-remote", "--refs", "origin", f"{ns}/*"], env=env)
    refs = [ln.split("\t", 1)[1] for ln in remote.stdout.splitlines() if "\t" in ln]
    if refs:
        r = _run(["git", "-C", top, "push", "-q", "--no-verify", "origin",
                  *[f":{ref}" for ref in refs]], env=env)
        if r.returncode != 0:
            log.warning("transfer.refs_delete_failed session=%s: %s", session_id, r.stderr.strip())
