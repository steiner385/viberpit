"""Shared-clone safety: a load-bearing clone must never be deleted, and a
deleted one must be recoverable (issue #1003).

Every session worktree under ``<repo_root>/.ccswitch/worktrees/<repo>/<id>``
holds only a ``.git`` *pointer file*::

    gitdir: <repo_root>/.ccswitch/clones/<owner>/<name>/.git/worktrees/<id>

so the shared clone is load-bearing for every session on the host.  On
2026-09-03 ``clones/cairnea/KinDash`` disappeared on two fleet hosts in the same
minute and 130 worktrees died at once with ``fatal: not a git repository`` —
working-tree files (including uncommitted work) intact, git metadata gone.

This module provides the three things that incident needed:

* :func:`diagnose` — which clones are missing while worktrees still reference
  them (and which links are broken for a clone that *does* exist).
* :func:`repair` / :func:`repair_worktree_links` — re-link orphaned worktrees to
  a (re-)cloned repo, preserving uncommitted work.
* :func:`remove_clone` — the ONLY sanctioned way to delete a clone: it refuses
  while any worktree still references it, and every outcome is journalled with
  the caller and the referencing-worktree count.

Why repair has to write git's admin files by hand
-------------------------------------------------
``git worktree repair`` only fixes *pointers between records that still exist*.
Once the clone is gone, a fresh clone has no ``.git/worktrees/<id>`` record at
all and repair fails with ``unable to locate repository; .git file does not
reference a repository`` (verified against real git, constitution III).  So
recovery recreates the minimal record git needs — ``gitdir``, ``commondir``,
``HEAD`` — then rebuilds the worktree index with a mixed ``git reset`` and lets
``git worktree repair`` normalise the result.

The branch each worktree was on is NOT recoverable: it lived only in the deleted
admin record (and in local ``ccswitch/*`` refs that died with the clone).
Recovered worktrees therefore land DETACHED at the remote default tip; the
files on disk — committed, modified, and untracked alike — are untouched.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ccswitch.gitrepos import clones_base
from ccswitch.worktrees import lock_worktree, worktrees_base

log = logging.getLogger(__name__)


class CloneInUseError(Exception):
    """Raised when a clone removal is refused because worktrees still use it."""


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True)


# ---------------------------------------------------------------------------
# Reading the worktree -> clone links
# ---------------------------------------------------------------------------


def read_gitdir_pointer(worktree_dir: str | os.PathLike[str]) -> str | None:
    """The path a worktree's ``.git`` pointer file references, or None.

    None when the dir has no ``.git`` file, has a real ``.git`` *directory* (a
    plain checkout, not a linked worktree), or the file is unreadable/malformed.
    """
    dot_git = Path(worktree_dir) / ".git"
    if not dot_git.is_file():
        return None
    try:
        text = dot_git.read_text(errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("gitdir:"):
            return line[len("gitdir:"):].strip() or None
    return None


def clone_dir_from_gitdir(gitdir: str) -> str | None:
    """The clone toplevel implied by a worktree ``gitdir`` pointer, or None.

    ``<clone>/.git/worktrees/<id>`` -> ``<clone>``.  Anything else (a bare repo,
    a hand-rolled layout) returns None rather than guessing.
    """
    p = Path(gitdir)
    parts = p.parts
    if len(parts) < 4 or parts[-2] != "worktrees" or parts[-3] != ".git":
        return None
    return str(Path(*parts[:-3]))


@dataclass(frozen=True)
class WorktreeLink:
    """One session worktree and the clone record it points at."""

    path: str            # the worktree directory
    gitdir: str          # what its .git pointer file references
    clone_dir: str       # clone toplevel implied by ``gitdir``
    record_present: bool  # whether that .git/worktrees/<id> record exists

    @property
    def worktree_id(self) -> str:
        return os.path.basename(self.path.rstrip("/"))


def scan_worktree_links(repo_root: str) -> list[WorktreeLink]:
    """Every ccswitch session worktree that points at a clone, sorted by path.

    Dirs with no usable ``.git`` pointer are skipped — they are not linked
    worktrees, so there is nothing to diagnose or repair about them.
    """
    base = worktrees_base(repo_root)
    links: list[WorktreeLink] = []
    if not base.is_dir():
        return links
    for repo_dir in sorted(base.iterdir()):
        if not repo_dir.is_dir():
            continue
        for wt_dir in sorted(repo_dir.iterdir()):
            if not wt_dir.is_dir():
                continue
            gitdir = read_gitdir_pointer(wt_dir)
            if not gitdir:
                continue
            clone_dir = clone_dir_from_gitdir(gitdir)
            if not clone_dir:
                continue
            links.append(
                WorktreeLink(
                    path=str(wt_dir),
                    gitdir=gitdir,
                    clone_dir=clone_dir,
                    record_present=os.path.isdir(gitdir),
                )
            )
    return links


def referencing_worktrees(repo_root: str, clone_dir: str | os.PathLike[str]) -> list[str]:
    """Paths of every session worktree whose ``.git`` points into ``clone_dir``.

    The load-bearing check behind :func:`remove_clone`: a non-empty result means
    deleting this clone kills that many live working trees.
    """
    target = os.path.normpath(str(clone_dir))
    return [
        link.path
        for link in scan_worktree_links(repo_root)
        if os.path.normpath(link.clone_dir) == target
    ]


def clone_worktree_records(clone_dir: str | os.PathLike[str]) -> list[str]:
    """Worktree ids git has admin records for under ``<clone>/.git/worktrees``.

    The clone's own view of what depends on it — complementary to
    :func:`referencing_worktrees` (the worktrees' view), because either side can
    survive the other.
    """
    records = Path(clone_dir) / ".git" / "worktrees"
    if not records.is_dir():
        return []
    return sorted(p.name for p in records.iterdir() if p.is_dir())


# ---------------------------------------------------------------------------
# Diagnosis
# ---------------------------------------------------------------------------


@dataclass
class CloneReport:
    """The health of one shared clone, from its worktrees' point of view."""

    clone_dir: str
    name_with_owner: str | None
    exists: bool
    links: list[WorktreeLink] = field(default_factory=list)

    @property
    def broken(self) -> list[WorktreeLink]:
        """Worktrees whose git metadata no longer resolves."""
        return [link for link in self.links if not link.record_present]

    @property
    def healthy(self) -> bool:
        return not self.broken


def name_with_owner_for(repo_root: str, clone_dir: str | os.PathLike[str]) -> str | None:
    """``<owner>/<name>`` for a clone under the ccswitch clones base, else None."""
    try:
        rel = Path(clone_dir).relative_to(clones_base(repo_root))
    except ValueError:
        return None
    if len(rel.parts) != 2:
        return None
    return f"{rel.parts[0]}/{rel.parts[1]}"


def diagnose(repo_root: str) -> list[CloneReport]:
    """Group every session worktree by the clone it depends on.

    One :class:`CloneReport` per referenced clone, sorted by clone path.  A
    report with ``exists=False`` is the #1003 shape — the clone is gone and every
    worktree under it is dead until :func:`repair` re-links them.
    """
    by_clone: dict[str, CloneReport] = {}
    for link in scan_worktree_links(repo_root):
        report = by_clone.get(link.clone_dir)
        if report is None:
            report = by_clone[link.clone_dir] = CloneReport(
                clone_dir=link.clone_dir,
                name_with_owner=name_with_owner_for(repo_root, link.clone_dir),
                exists=os.path.isdir(os.path.join(link.clone_dir, ".git")),
            )
        report.links.append(link)
    return [by_clone[key] for key in sorted(by_clone)]


# ---------------------------------------------------------------------------
# Repair
# ---------------------------------------------------------------------------


def _base_sha(clone_dir: str, base_ref: str | None) -> str | None:
    """SHA to detach a recovered worktree at: ``base_ref``, origin/HEAD, or HEAD."""
    candidates = [base_ref] if base_ref else ["origin/HEAD", "HEAD"]
    for ref in candidates:
        r = _run(["git", "-C", clone_dir, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"])
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    return None


def repair_worktree_links(
    clone_dir: str,
    worktree_paths: list[str],
    *,
    base_ref: str | None = None,
    force: bool = False,
) -> dict[str, str]:
    """Re-link orphaned worktrees to ``clone_dir``; return ``{path: outcome}``.

    Outcome is ``"repaired"``, ``"skipped"`` (nothing was broken), or an error
    string. ``force`` rewrites the record even when one is already present — for
    paths the caller diagnosed as broken BEFORE it re-cloned, so an explicit
    ``base_ref`` still applies after :func:`gitrepos.ensure_clone`'s own
    self-heal has run.  For each broken worktree we recreate the admin record git lost
    (``gitdir``/``commondir``/``HEAD``), rewrite the worktree's own ``.git``
    pointer (so a clone that came back under a different path/case still works),
    rebuild the index with a mixed ``git reset``, normalise via ``git worktree
    repair``, and re-lock the worktree (#177 — an unlocked record can be reaped
    by a sibling's prune).

    The working tree is never touched: a mixed reset moves only the index, so
    uncommitted modifications and untracked files survive.  Recovered worktrees
    are DETACHED at ``base_ref`` (default: the remote default tip) because the
    branch they were on died with the clone's refs.
    """
    results: dict[str, str] = {}
    sha = _base_sha(clone_dir, base_ref)
    if sha is None:
        for path in worktree_paths:
            results[path] = "error: clone has no resolvable base commit"
        return results

    for path in worktree_paths:
        wt = os.path.abspath(path)
        wt_id = os.path.basename(wt.rstrip("/"))
        record = Path(clone_dir) / ".git" / "worktrees" / wt_id
        if not force and record.is_dir() and read_gitdir_pointer(wt) == str(record):
            results[path] = "skipped"
            continue
        try:
            record.mkdir(parents=True, exist_ok=True)
            (record / "gitdir").write_text(f"{os.path.join(wt, '.git')}\n")
            (record / "commondir").write_text("../..\n")
            (record / "HEAD").write_text(f"{sha}\n")
            Path(wt, ".git").write_text(f"gitdir: {record}\n")
        except OSError as exc:
            results[path] = f"error: {exc}"
            continue

        # Rebuild the index from HEAD. Without it every tracked file reads as a
        # staged deletion (empty index) even though the file is right there.
        reset = _run(["git", "-C", wt, "reset", "-q"])
        if reset.returncode != 0:
            results[path] = f"error: git reset failed: {reset.stderr.strip()}"
            continue
        # Best-effort normalisation; the records above are already consistent.
        fix = _run(["git", "-C", clone_dir, "worktree", "repair", wt])
        if fix.returncode != 0:
            log.warning("clone.repair_normalise_failed path=%s err=%s", wt, fix.stderr.strip())
        lock_worktree(clone_dir, Path(wt))
        log.info("clone.worktree_relinked clone=%s path=%s head=%s", clone_dir, wt, sha)
        results[path] = "repaired"
    return results


def default_clone_url(name_with_owner: str) -> str:
    """The URL a missing clone is re-created from: the GitHub HTTPS remote.

    Reconstructed rather than read back, because the only place the real remote
    URL was recorded — the clone's own ``.git/config`` — died with the clone.
    Authentication is unchanged: ``ensure_clone`` mints the owner's App
    installation token for the fetch (#887).
    """
    return f"https://github.com/{name_with_owner}.git"


def repair(
    repo_root: str,
    *,
    dry_run: bool = False,
    base_ref: str | None = None,
    url_for: "Callable[[str], str] | None" = None,
) -> list[dict]:
    """Recover every clone that worktrees still depend on but that is missing.

    Re-clones through ``gitrepos.ensure_clone`` (a no-op when the clone is
    already there) and re-links the orphaned worktrees.  Returns one dict per
    affected clone: ``{clone, repo, action, worktrees, results}``.

    ``dry_run`` reports what would happen and changes nothing.

    ``url_for`` maps ``<owner>/<name>`` to the URL to re-clone from, defaulting
    to :func:`default_clone_url`. It is the seam for a repo whose remote is not
    the public GitHub HTTPS URL — a self-hosted remote, or a test fixture —
    without which recovery would be hard-wired to github.com.
    """
    from ccswitch import gitrepos  # deferred: gitrepos calls back into this module

    out: list[dict] = []
    for report in diagnose(repo_root):
        broken = [link.path for link in report.broken]
        if not broken:
            continue
        entry: dict = {
            "clone": report.clone_dir,
            "repo": report.name_with_owner,
            "cloneExists": report.exists,
            "worktrees": broken,
        }
        if dry_run:
            entry["action"] = "would-repair"
            out.append(entry)
            continue
        if not report.exists:
            if not report.name_with_owner:
                entry["action"] = "skipped"
                entry["error"] = "clone path is outside the ccswitch clones base"
                out.append(entry)
                continue
            url = (url_for or default_clone_url)(report.name_with_owner)
            try:
                gitrepos.ensure_clone(repo_root, report.name_with_owner, url)
            except gitrepos.CloneError as exc:
                entry["action"] = "failed"
                entry["error"] = str(exc)
                out.append(entry)
                continue
        entry["action"] = "repaired"
        # force: these paths were diagnosed broken before the re-clone above, and
        # ensure_clone's self-heal may already have re-linked them at origin/HEAD
        # — rewriting is idempotent and is what honours an explicit ``base_ref``.
        entry["results"] = repair_worktree_links(
            report.clone_dir, broken, base_ref=base_ref, force=True)
        out.append(entry)
    return out


# ---------------------------------------------------------------------------
# Guarded removal + audit (asks 2 and 4 of #1003)
# ---------------------------------------------------------------------------


def _caller() -> str:
    """``file:line`` of the first frame outside this module — for the audit line."""
    here = os.path.abspath(__file__)
    for frame in reversed(traceback.extract_stack()[:-1]):
        if os.path.abspath(frame.filename) != here:
            return f"{os.path.basename(frame.filename)}:{frame.lineno}"
    return "unknown"


def remove_clone(
    repo_root: str,
    clone_dir: str | os.PathLike[str],
    *,
    reason: str,
    force: bool = False,
) -> dict:
    """Delete a ccswitch-owned clone, refusing while worktrees still need it.

    A shared clone is load-bearing: removing one with live worktrees kills every
    session under it (#1003).  So this refuses — raising :class:`CloneInUseError`
    — when the clone still has ``.git/worktrees`` records OR any session worktree
    points at it, unless the caller passes ``force``.

    Every outcome is journalled (``clone.remove`` / ``clone.remove_blocked`` /
    ``clone.remove_forced``) with the reason, the caller, and the
    referencing-worktree count, so a deletion is never invisible again.

    Refuses outright — never forceable — for a path outside
    ``<repo_root>/.ccswitch/clones``: ccswitch must never delete a user checkout.
    """
    path = Path(clone_dir).expanduser()
    resolved = str(path)
    repo = name_with_owner_for(repo_root, resolved)
    caller = _caller()
    if repo is None:
        log.error("clone.remove_blocked path=%s reason=%s caller=%s cause=outside-clones-base",
                  resolved, reason, caller)
        raise CloneInUseError(
            f"refusing to remove {resolved!r}: not a ccswitch clone under "
            f"{clones_base(repo_root)}"
        )

    refs = referencing_worktrees(repo_root, resolved)
    records = clone_worktree_records(resolved)
    if (refs or records) and not force:
        log.error(
            "clone.remove_blocked path=%s repo=%s reason=%s caller=%s "
            "worktrees=%d records=%d",
            resolved, repo, reason, caller, len(refs), len(records),
        )
        raise CloneInUseError(
            f"refusing to remove clone {repo} at {resolved}: {len(refs)} session "
            f"worktree(s) and {len(records)} git worktree record(s) still reference it"
        )

    if not path.exists():
        log.info("clone.remove_absent path=%s repo=%s reason=%s caller=%s",
                 resolved, repo, reason, caller)
        return {"clone": resolved, "repo": repo, "removed": False,
                "worktrees": len(refs), "records": len(records)}

    level = log.warning if (refs or records) else log.info
    level(
        "clone.%s path=%s repo=%s reason=%s caller=%s worktrees=%d records=%d",
        "remove_forced" if (refs or records) else "remove",
        resolved, repo, reason, caller, len(refs), len(records),
    )
    shutil.rmtree(path)
    return {"clone": resolved, "repo": repo, "removed": True,
            "worktrees": len(refs), "records": len(records)}
