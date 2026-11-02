"""A session worktree's files, listed and read — the data behind ``@``-file mentions,
the read-only Files tab and clickable transcript paths (D-7).

Runs in the credential-holding daemon against an agent-controlled worktree, so it
shares :mod:`viberpit.worktree_diff`'s threat model and hardened git invocation (no
system config, no hooks, no fsmonitor, repo-redirect env stripped).

- :func:`list_files` streams ``git ls-files --cached --others --exclude-standard -z``
  and stops after ``MAX_SCAN_ENTRIES``, so the work is bounded however large the tree.
  Ranking (fuzzy, or by mtime for an empty query) only ever touches that capped set.
- :func:`read_file` resolves the path with ``patterns.worktree_rel`` (realpath +
  ``commonpath``; fails closed on ``..``, ``~``, NUL), refuses ``.git`` internals, then
  opens it component by component with ``O_NOFOLLOW`` from the worktree's directory
  fd — so a directory swapped for an outside symlink after the check is never
  followed — non-blocking, and reads only what ``fstat`` says is a regular file.

Synchronous by design — callers run it via ``asyncio.to_thread``.
"""

from __future__ import annotations

import errno
import heapq
import os
import selectors
import stat
import subprocess
import time
from typing import Any

from server.permission_rules.patterns import worktree_rel
from viberpit.worktree_diff import (
    BINARY_SNIFF_BYTES,
    GIT_GLOBAL,
    GIT_TIMEOUT_S,
    GitTimeout,
    git_env,
    worktree_top,
)

MAX_SCAN_ENTRIES = 20_000
DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 200
MAX_READ_BYTES = 512 * 1024

PATH_OUTSIDE_WORKTREE = "PATH_OUTSIDE_WORKTREE"
FILE_NOT_FOUND = "FILE_NOT_FOUND"

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


class FileAccessError(Exception):
    """A file read refused: ``code`` is ``PATH_OUTSIDE_WORKTREE`` (400) or
    ``FILE_NOT_FOUND`` (404).

    ``error_data`` rides the daemon's RPC error envelope, so a remote edge rebuilds it
    with its code (``RemoteOrchestrator.session_file``), and marks it an expected
    refusal the daemon logs at INFO rather than a handler defect."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code
        self.error_data = {"code": code}


# ------------------------------------------------------------------ listing


def _scan(top: str) -> tuple[list[str], bool]:
    """Up to ``MAX_SCAN_ENTRIES`` distinct ``/``-separated paths under ``top``, and
    whether the listing was cut (more entries, or git ran past its timeout).

    Streamed: git is killed as soon as the cap is passed. Paths that are not valid
    UTF-8 are skipped — they could not round-trip through JSON to a read."""
    try:
        proc = subprocess.Popen(
            ["git", "-C", top, *GIT_GLOBAL, "ls-files", "--cached", "--others",
             "--exclude-standard", "-z"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=git_env(),
        )
    except OSError:
        return [], False
    assert proc.stdout is not None
    cap = MAX_SCAN_ENTRIES
    seen: dict[str, None] = {}  # insertion-ordered set: unmerged entries repeat
    pending = b""
    cut = failed = False
    deadline = time.monotonic() + GIT_TIMEOUT_S
    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ)
    try:
        while not cut:
            left = deadline - time.monotonic()
            if left <= 0 or not sel.select(left):
                cut = True  # timed out: what was read is a lower bound
                break
            chunk = os.read(proc.stdout.fileno(), 65536)
            if not chunk:
                break
            *toks, pending = (pending + chunk).split(b"\0")
            for tok in toks:
                # A trailing "/" is an untracked nested repository, not a file.
                if not tok or tok.endswith(b"/"):
                    continue
                try:
                    path = tok.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                if path in seen:
                    continue
                if len(seen) >= cap:
                    cut = True
                    break
                seen[path] = None
        if not cut:
            try:
                failed = proc.wait(timeout=max(deadline - time.monotonic(), 0)) != 0
            except subprocess.TimeoutExpired:
                cut = True
    finally:
        sel.close()
        if proc.poll() is None:
            proc.kill()
        proc.stdout.close()
        proc.wait()
    if failed:
        return [], False
    return list(seen), cut


def _runs(q: str, s: str) -> int | None:
    """Fewest contiguous runs in which ``q`` occurs as a subsequence of ``s`` (both
    lowercased), trying each start of ``q[0]`` with a greedy tail; None if no match."""
    if q in s:
        return 1
    best: int | None = None
    start = s.find(q[0])
    while start != -1:
        runs, prev = 1, start
        for ch in q[1:]:
            k = s.find(ch, prev + 1)
            if k == -1:
                return best  # a later start cannot match either
            if k != prev + 1:
                runs += 1
            prev = k
        best = runs if best is None else min(best, runs)
        start = s.find(q[0], start + 1)
    return best


def _rank_key(q: str, path: str) -> tuple[int, int, int, str] | None:
    """Sort key (lower is better): a basename match beats a directory match, fewer
    runs beat scattered ones, shorter paths win ties. None when ``q`` does not match."""
    low = path.lower()
    base = low.rsplit("/", 1)[-1]
    runs = _runs(q, base)
    if runs is not None:
        return 0, runs, len(path), path
    runs = _runs(q, low)
    if runs is not None:
        return 1, runs, len(path), path
    return None


def list_files(cwd: str, q: str = "", limit: int = DEFAULT_LIST_LIMIT) -> dict[str, Any]:
    """``{files: [{path}], truncated}`` for the work tree containing ``cwd``.

    Tracked and untracked non-ignored files, paths relative to the work-tree top level.
    A non-empty ``q`` is a case-insensitive fuzzy (subsequence) filter, best first; an
    empty one lists the most recently modified first. ``limit`` is clamped to
    ``[1, MAX_LIST_LIMIT]``. ``truncated`` means only the first ``MAX_SCAN_ENTRIES``
    entries were considered. A missing, empty or non-git ``cwd`` lists nothing.
    """
    limit = max(1, min(int(limit), MAX_LIST_LIMIT))
    try:
        top = worktree_top(cwd)
    except GitTimeout:
        return {"files": [], "truncated": True}
    if top is None:
        return {"files": [], "truncated": False}
    paths, truncated = _scan(top)
    q = q.strip().lower()
    if q:
        keyed = (k for p in paths if (k := _rank_key(q, p)) is not None)
        ranked = [k[3] for k in heapq.nsmallest(limit, keyed)]
    else:
        stamped: list[tuple[int, str]] = []
        for p in paths:
            try:
                st = os.lstat(os.path.join(top, p))
            except OSError:  # deleted but still in the index
                continue
            stamped.append((-st.st_mtime_ns, p))
        ranked = [p for _, p in heapq.nsmallest(limit, stamped)]
    return {"files": [{"path": p} for p in ranked], "truncated": truncated}


# ------------------------------------------------------------------ reading


def _is_git_internal(rel: str) -> bool:
    # Case-insensitive: on case-insensitive filesystems ``.GIT`` is ``.git``.
    return any(part.lower() == ".git" for part in rel.split("/"))


def _open_beneath(top: str, rel: str) -> int:
    """Open ``top/rel`` read-only and non-blocking without following any symlink on
    the way, from ``top``'s directory fd. Raises ``FileAccessError``."""
    parts = rel.split("/")
    try:
        dfd = os.open(top, os.O_RDONLY | os.O_DIRECTORY | _CLOEXEC)
    except OSError as exc:
        raise FileAccessError(FILE_NOT_FOUND) from exc
    try:
        for part in parts[:-1]:
            try:
                nfd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW | _CLOEXEC,
                              dir_fd=dfd)
            except OSError as exc:
                raise _open_error(exc, part, dfd) from exc
            os.close(dfd)
            dfd = nfd
        try:
            return os.open(parts[-1], os.O_RDONLY | os.O_NONBLOCK | _NOFOLLOW | _CLOEXEC,
                           dir_fd=dfd)
        except OSError as exc:
            raise _open_error(exc, parts[-1], dfd) from exc
    finally:
        os.close(dfd)


def _open_error(exc: OSError, name: str, dfd: int) -> FileAccessError:
    if exc.errno == errno.ENOENT:
        return FileAccessError(FILE_NOT_FOUND)
    if exc.errno == errno.ENOTDIR:
        # O_DIRECTORY|O_NOFOLLOW on a symlink is ENOTDIR too: that one is an escape.
        try:
            is_link = stat.S_ISLNK(os.lstat(name, dir_fd=dfd).st_mode)
        except OSError:
            is_link = False
        return FileAccessError(PATH_OUTSIDE_WORKTREE if is_link else FILE_NOT_FOUND)
    return FileAccessError(PATH_OUTSIDE_WORKTREE)


def read_file(cwd: str, path: str) -> dict[str, Any]:
    """``{path, text, binary, truncated, size}`` of a regular file in ``cwd``'s work tree.

    ``path`` is relative to the work-tree top level (or absolute inside it); the
    returned ``path`` is its resolved top-level-relative form. At most
    ``MAX_READ_BYTES`` are returned (``truncated`` beyond); a NUL in the first
    ``BINARY_SNIFF_BYTES`` makes it ``binary`` with ``text: None``. ``size`` is the
    file's size on disk.

    Raises ``FileAccessError(PATH_OUTSIDE_WORKTREE)`` for anything resolving outside
    the work tree, into ``.git``, or to something other than a regular file (a FIFO
    never blocks), and ``FileAccessError(FILE_NOT_FOUND)`` for a missing file or a
    missing, empty or non-git ``cwd``.
    """
    try:
        top = worktree_top(cwd)
    except GitTimeout:
        top = None
    if top is None:
        raise FileAccessError(FILE_NOT_FOUND)
    rel = worktree_rel(path, top) if isinstance(path, str) else None
    if rel is None or _is_git_internal(rel):
        raise FileAccessError(PATH_OUTSIDE_WORKTREE)
    fd = _open_beneath(top, rel)
    try:
        st = os.fstat(fd)
    except OSError:
        os.close(fd)
        raise
    if not stat.S_ISREG(st.st_mode):
        os.close(fd)
        raise FileAccessError(PATH_OUTSIDE_WORKTREE)
    with os.fdopen(fd, "rb") as fh:
        data = fh.read(MAX_READ_BYTES + 1)
    truncated = len(data) > MAX_READ_BYTES
    data = data[:MAX_READ_BYTES]
    binary = b"\0" in data[:BINARY_SNIFF_BYTES]
    return {
        "path": rel,
        "text": None if binary else data.decode("utf-8", "replace"),
        "binary": binary,
        "truncated": truncated,
        "size": max(st.st_size, len(data)),
    }


__all__ = [
    "DEFAULT_LIST_LIMIT", "FILE_NOT_FOUND", "MAX_LIST_LIMIT", "MAX_READ_BYTES",
    "MAX_SCAN_ENTRIES", "PATH_OUTSIDE_WORKTREE", "FileAccessError", "list_files",
    "read_file",
]
