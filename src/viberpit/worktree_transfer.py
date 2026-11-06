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
    RECOVERY_FORCED, WorktreeInfo, _origin_url, _run, _short, _try_fetch_origin,
    authored_branches, clear_stale_registration, current_branch, lock_worktree,
    remove_worktree, snapshot_recovery_ref, worktrees_base,
)

log = logging.getLogger(__name__)

__all__ = ["TransferError", "WorktreeState", "Bundle", "Finalized", "TRANSFER_REF_NS",
           "DIRTY_PATHS_CAP", "head_ref", "branch_ref", "inspect", "export", "import_",
           "unpack_wip", "finalize", "delete_transfer_refs"]

#: Hidden ref namespace on origin. Not under refs/heads, so it never appears as a
#: branch, never triggers CI or PR automation, and is deleted after import.
TRANSFER_REF_NS = "refs/viberpit/transfer"
WIP_SUBJECT = "viberpit: transfer WIP for session {session_id}; not for merge"
_WIP_IDENTITY = ["-c", "user.name=ViberPit", "-c", "user.email=viberpit@localhost"]
#: At most this many dirty paths are listed (``WorktreeState.dirty_paths``) — what the
#: move dialog shows before asking for consent. ``dirty_files`` keeps the full count.
DIRTY_PATHS_CAP = 50


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
    #: The first ``DIRTY_PATHS_CAP`` dirty paths, as ``git status --porcelain`` names them.
    dirty_paths: tuple[str, ...] = ()
    #: Commits an export would push that no ``origin`` remote-tracking ref reaches (from
    #: HEAD and the ``unpushed`` branches) — what a transfer ref newly exposes. Pushed
    #: refs are deleted after import, but the commits stay fetchable by SHA, so on a
    #: public repo the caller refuses a move with any (D-17 final review).
    unpushed_commits: int = 0


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
    paths = tuple(_porcelain_path(ln) for ln in lines[:DIRTY_PATHS_CAP])
    return WorktreeState(dirty=bool(lines), dirty_files=len(lines), branch=branch,
                         head_sha=head, unpushed=tuple(unpushed), dirty_paths=paths,
                         unpushed_commits=_count_unpushed(cwd, head, unpushed))


def _count_unpushed(cwd: str, head: str, branches: list[str]) -> int:
    """Commits reachable from *head* or *branches* but from no ``origin`` ref. A failed
    count is reported as 1 (fail closed: the caller treats it as unpushed work)."""
    tips = [head, *(f"refs/heads/{b}" for b in branches)]
    r = _run(["git", "-C", cwd, "rev-list", "--count", *tips, "--not", "--remotes=origin"])
    try:
        return int(r.stdout.strip()) if r.returncode == 0 else 1
    except ValueError:
        return 1


def _porcelain_path(line: str) -> str:
    """The path of one ``git status --porcelain`` line (the new name of a rename)."""
    path = line[3:] if len(line) > 3 else line.strip()
    return path.split(" -> ", 1)[-1]


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


def export(cwd: str, session_id: str, *, carry_dirty: bool,
           vetted: WorktreeState | None = None) -> Bundle:
    """Push *cwd*'s HEAD and unpushed authored branches to the transfer namespace.

    A dirty tree is refused unless ``carry_dirty`` (the user ticked the consent
    box; the rebalancer never passes True) AND the caller hands back the *vetted*
    state its policy approved — the consent, and the repo-visibility check behind it
    (D-17 revised D1: never on a public repo), were about THAT tree. A tree that is
    not exactly the vetted one any more (dirtied after a clean vet, or new files
    since) is refused with ``dirty_worktree`` and nothing is committed or pushed, so
    no caller can WIP-commit bytes nobody vetted. With consent the edits become one
    WIP commit on top of HEAD, pushed as the transfer head; on a push failure that
    commit is undone (``reset --mixed``) so the worktree is exactly as found.

    Committed-but-unpushed work gets the same rule when *vetted* is given: the caller's
    visibility policy approved exactly the vetted state's ``unpushed_commits`` (none on a
    public repo), so a tree with unpushed commits it did not see — a new commit, a moved
    HEAD — is refused with ``unpushed_commits`` and nothing is pushed.
    """
    top = _repo_top(cwd)
    url = _origin_url(top)
    if not url:
        raise TransferError("no_origin", f"{top} has no origin remote")
    _try_fetch_origin(top)  # best-effort: makes `unpushed` accurate against a fresh origin
    state = inspect(cwd)
    if state.dirty and not carry_dirty:
        raise TransferError("dirty_worktree", f"{state.dirty_files} uncommitted change(s)")
    if state.dirty and (vetted is None or not vetted.dirty
                        or vetted.dirty_files != state.dirty_files
                        or vetted.dirty_paths != state.dirty_paths
                        or vetted.head_sha != state.head_sha):
        raise TransferError("dirty_worktree",
                            "the worktree changed since it was vetted; nothing was pushed")
    if vetted is not None and state.unpushed_commits and (
            not vetted.unpushed_commits or vetted.head_sha != state.head_sha
            or state.unpushed_commits > vetted.unpushed_commits
            or not set(state.unpushed) <= set(vetted.unpushed)):
        raise TransferError("unpushed_commits",
                            f"{state.unpushed_commits} unpushed commit(s) were not vetted; "
                            "nothing was pushed")
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
    HEAD is the bundle's head or base is reused — and a WIP commit export left at its
    HEAD is unpacked again (``unpack_wip``), so the agent's branch never keeps the
    transport commit and the edits are back as unstaged changes. A branch already checked
    out elsewhere on this clone is refused rather than renamed — ``create_worktree``'s
    child-branch fallback would silently change the branch the agent remembers.

    ``is_child`` is True only when this call created the branch (no local ref before
    it), never for a reused worktree or a branch the clone already had.
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
            unpack_wip(str(dest), bundle.session_id, bundle)
            log.info("transfer.import reuse path=%s", dest)
            return WorktreeInfo(str(dest), bundle.branch, is_child=False, repo=top)
        raise TransferError("dest_occupied", f"{dest} exists with a different HEAD")

    dest.parent.mkdir(parents=True, exist_ok=True)
    clear_stale_registration(top, dest)
    made_branch = False   # this call created refs/heads/<branch> (no prior ref)
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
        made_branch = prev is None
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
    return WorktreeInfo(str(dest), bundle.branch, is_child=made_branch, repo=top)


def _wip_parent(cwd: str, session_id: str, bundle: Bundle | None) -> str | None:
    """The commit a WIP transport commit at *cwd*'s HEAD sits on, or None when HEAD is
    not this session's WIP commit. With a bundle the answer is exact (HEAD is the
    bundle's WIP head); without one (crash reconcile) the WIP is recognised by the
    subject ``export`` gave it — scoped to *session_id*, so another session's WIP is
    never touched."""
    head = _rev(cwd, "HEAD")
    if head is None:
        return None
    if bundle is not None:
        return bundle.base_sha if bundle.wip_commit and head == bundle.head_sha else None
    subject = _run(["git", "-C", cwd, "log", "-1", "--format=%s", "HEAD"])
    if subject.returncode != 0 or subject.stdout.strip() != WIP_SUBJECT.format(
            session_id=session_id):
        return None
    return _rev(cwd, "HEAD^")


def unpack_wip(cwd: str, session_id: str, bundle: Bundle | None = None) -> bool:
    """Undo the WIP transport commit at *cwd*'s HEAD (``reset --mixed`` onto its
    parent): the edits come back as unstaged changes and the branch tip is where the
    agent left it. The restore-to-source path, and the reuse branch of ``import_``.
    Returns True iff a WIP commit was unpacked; idempotent."""
    if not os.path.isdir(cwd):
        return False
    parent = _wip_parent(cwd, session_id, bundle)
    if parent is None:
        return False
    r = _run(["git", "-C", cwd, "reset", "-q", "--mixed", parent])
    if r.returncode != 0:
        raise TransferError("wip_unpack_failed", r.stderr.strip())
    log.info("transfer.wip_unpacked session=%s path=%s", session_id, cwd)
    return True


@dataclass(frozen=True)
class Finalized:
    """What ``finalize`` did. Truthy iff the worktree directory was removed."""

    removed: bool
    recovery_ref: str | None = None

    def __bool__(self) -> bool:
        return self.removed


def finalize(bundle: Bundle | None, worktree_path: str, *,
             session_id: str | None = None) -> Finalized:
    """Drop this host's copy of a session worktree that now lives on another host:
    the source after a successful move, or a target whose import was abandoned.

    The WIP commit was a transport artifact: put the branch tip back at the base
    (``reset --soft`` keeps the edits staged), then take a recovery snapshot of the
    full tree (``refs/viberpit/recovery/<sid>/…``, local) BEFORE the forced removal,
    so a bad move stays recoverable here. *bundle* may be None (crash reconcile); the
    WIP is then found by its subject and *session_id* is required. Safe to call twice.
    """
    sid = bundle.session_id if bundle is not None else session_id
    if not sid:
        raise ValueError("finalize needs a bundle or a session_id")
    if not os.path.isdir(worktree_path):
        return Finalized(False)
    parent = _wip_parent(worktree_path, sid, bundle)
    if parent is not None:
        _run(["git", "-C", worktree_path, "reset", "-q", "--soft", parent])
    ref = snapshot_recovery_ref(worktree_path, sid, reason=RECOVERY_FORCED)
    if ref is None:
        # None means "clean, nothing to keep" OR "the snapshot failed": tell them apart.
        status = _run(["git", "-C", worktree_path, "status", "--porcelain"])
        if status.returncode != 0 or status.stdout.strip():
            log.warning("transfer.finalize_refused session=%s path=%s — the tree has "
                        "uncommitted changes and no recovery snapshot could be taken; "
                        "leaving it in place", sid, worktree_path)
            return Finalized(False)
    removed = remove_worktree(worktree_path, force=True, session_id=sid)
    log.info("transfer.finalize session=%s removed=%s path=%s recovery_ref=%s",
             sid, removed, worktree_path, ref)
    return Finalized(removed, ref)


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
