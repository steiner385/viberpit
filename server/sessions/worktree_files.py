"""A session worktree's files, listed and read — the data behind ``@``-file mentions,
the read-only Files tab and clickable transcript paths (D-7).

Runs in the credential-holding daemon against an agent-controlled worktree, so it
shares :mod:`viberpit.worktree_diff`'s threat model and hardened git invocation (no
system config, no hooks, no fsmonitor, repo-redirect env stripped).

Paths are relative to the session's **cwd**, not the work-tree top level: that is how
the CLI resolves ``@path``, so a session started in a subdirectory lists, mentions and
reads exactly the files beneath it, and nothing above it.

- :func:`list_files` streams ``git ls-files --cached --others --exclude-standard -z``
  run in the cwd (git itself makes the paths cwd-relative) and stops after
  ``MAX_SCAN_ENTRIES``, so the work is bounded however large the tree. Ranking (fuzzy,
  or by mtime for an empty query) only ever touches that capped set, and is bounded
  too: ``q`` is at most ``MAX_QUERY_CHARS``, a path is ranked on at most its last
  ``RANK_TAIL_CHARS``, a scattered match tries at most ``MAX_MATCH_STARTS`` starts,
  and the pass stops at ``RANK_DEADLINE_S`` with what it has (``truncated``).
- :func:`read_file` resolves the path with ``patterns.worktree_rel`` against the cwd
  (realpath + ``commonpath``; fails closed on ``..``, ``~``, NUL), refuses ``.git``
  internals at any depth, then opens it component by component with ``O_NOFOLLOW``
  from the cwd's directory fd — so a directory swapped for an outside symlink after
  the check is never followed — non-blocking, and reads only what ``fstat`` says is a
  regular file.

Accepted limit: a hardlink the agent made to an outside file is read (it can read that file already).

Synchronous by design — callers run it via ``asyncio.to_thread``.
"""

from __future__ import annotations

import errno
import heapq
import os
import stat
import time
from typing import Any

from server.permission_rules.patterns import worktree_rel
from viberpit.worktree_diff import (
    BINARY_SNIFF_BYTES,
    GitTimeout,
    stream_git,
    worktree_top,
)

MAX_SCAN_ENTRIES = 20_000
DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 200
MAX_READ_BYTES = 512 * 1024
# Ranking bounds: paths are agent-controlled, so a crafted long path and a long query
# must not make the subsequence match quadratic across the whole scanned set.
MAX_QUERY_CHARS = 64
LONG_PATH_CHARS = 1024     # a path longer than this is ranked on its tail only
RANK_TAIL_CHARS = 512
MAX_MATCH_STARTS = 16
RANK_DEADLINE_S = 0.25

PATH_OUTSIDE_WORKTREE = "PATH_OUTSIDE_WORKTREE"
FILE_NOT_FOUND = "FILE_NOT_FOUND"
# ``error_data`` code of the KeyError for an unknown session on any worktree read.
SESSION_NOT_FOUND = "SESSION_NOT_FOUND"

_clock = time.monotonic  # the ranking deadline's clock (a test seam)


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


def _scan(cwd: str) -> tuple[list[str], bool]:
    """Up to ``MAX_SCAN_ENTRIES`` distinct ``/``-separated paths under ``cwd``, relative
    to it, and whether the listing was cut (more entries, or git ran past its timeout).

    Streamed: git is killed as soon as the cap is passed. Paths that are not valid
    UTF-8 are skipped — they could not round-trip through JSON to a read."""
    cap = MAX_SCAN_ENTRIES
    seen: dict[str, None] = {}  # insertion-ordered set: unmerged entries repeat
    pending = b""

    def consume(chunk: bytes) -> bool:
        nonlocal pending
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
                return True
            seen[path] = None
        return False

    status = stream_git(cwd, ["ls-files", "--cached", "--others", "--exclude-standard",
                              "-z"], consume)
    if status == "failed":
        return [], False
    # A timeout is a cut too: what was read is a lower bound.
    return list(seen), status != "ok"


_WORD_SEPS = "_-./ "


def _at_word_start(orig: str, low: str, i: int) -> bool:
    """Whether index ``i`` of a basename starts a word: after a separator, or a
    camelCase hump (an upper-case letter after a lower-case letter or digit)."""
    if i == 0 or low[i - 1] in _WORD_SEPS:
        return True
    return (len(orig) == len(low) and orig[i].isupper()
            and (orig[i - 1].islower() or orig[i - 1].isdigit()))


def _runs(q: str, s: str) -> int | None:
    """Fewest contiguous runs in which ``q`` occurs as a subsequence of ``s`` (both
    lowercased), trying at most ``MAX_MATCH_STARTS`` starts of ``q[0]`` with a greedy
    tail; None if no match."""
    best: int | None = None
    start = s.find(q[0])
    tries = 0
    while start != -1 and tries < MAX_MATCH_STARTS:
        tries += 1
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


def _rank_key(q: str, path: str) -> tuple[int, int, int, int, str] | None:
    """Sort key (lower is better) of ``path`` for the lowercased query ``q``; None when
    ``q`` does not match. Tiers: (0) the basename starts with ``q``; (1) ``q`` starts a
    word in the basename (after ``_ - . /`` or a camelCase hump); (2) ``q`` elsewhere in
    the basename; (3) ``q`` in a directory; (4) a scattered subsequence, fewer runs
    first. Within a tier the shorter basename stem (``q`` covers more of the name),
    then the shorter path, wins.

    A path over ``LONG_PATH_CHARS`` is matched on its last ``RANK_TAIL_CHARS`` only."""
    orig = path if len(path) <= LONG_PATH_CHARS else path[-RANK_TAIL_CHARS:]
    low = orig.lower()
    cut = low.rfind("/") + 1
    base, base_orig = low[cut:], orig[cut:] if len(orig) == len(low) else ""
    stem = len(base.lstrip(".").split(".", 1)[0])
    tier: int | None = None
    runs = 0
    i = base.find(q)
    if i == 0:
        tier = 0
    elif i > 0:
        tier = 2
        for _ in range(MAX_MATCH_STARTS):
            if i == -1:
                break
            if _at_word_start(base_orig or base, base, i):
                tier = 1
                break
            i = base.find(q, i + 1)
    elif q in low[:cut]:
        tier = 3
    else:
        r = _runs(q, low)
        if r is None:
            return None
        tier, runs = 4, r
    return tier, runs, stem, len(path), path


def _rank(q: str, paths: list[str], limit: int) -> tuple[list[str], bool]:
    """The best ``limit`` of ``paths`` for ``q``, and whether the pass ran out of time
    (``RANK_DEADLINE_S``) — then only the paths ranked so far were considered."""
    deadline = _clock() + RANK_DEADLINE_S
    keyed: list[tuple[int, int, int, int, str]] = []
    late = False
    for p in paths:
        if _clock() > deadline:
            late = True
            break
        k = _rank_key(q, p)
        if k is not None:
            keyed.append(k)
    return [k[-1] for k in heapq.nsmallest(limit, keyed)], late


def list_files(cwd: str, q: str = "", limit: int = DEFAULT_LIST_LIMIT) -> dict[str, Any]:
    """``{files: [{path}], truncated}`` beneath ``cwd`` in its git work tree.

    Tracked and untracked non-ignored files under ``cwd``, paths relative to ``cwd``
    (as the CLI resolves ``@path``). A non-empty ``q`` (cut to ``MAX_QUERY_CHARS``) is
    a case-insensitive fuzzy filter, best first (see :func:`_rank_key`); an empty one
    lists the most recently modified first. ``limit`` is clamped to
    ``[1, MAX_LIST_LIMIT]``. ``truncated`` means not every entry was considered: only
    the first ``MAX_SCAN_ENTRIES``, or ranking hit ``RANK_DEADLINE_S``. A missing,
    empty or non-git ``cwd`` lists nothing.
    """
    limit = max(1, min(int(limit), MAX_LIST_LIMIT))
    try:
        top = worktree_top(cwd)
    except GitTimeout:
        return {"files": [], "truncated": True}
    if top is None:
        return {"files": [], "truncated": False}
    paths, truncated = _scan(cwd)
    q = q.strip().lower()[:MAX_QUERY_CHARS]
    if q:
        ranked, late = _rank(q, paths, limit)
        truncated = truncated or late
    else:
        stamped: list[tuple[int, str]] = []
        for p in paths:
            try:
                st = os.lstat(os.path.join(cwd, p))
            except OSError:  # deleted but still in the index
                continue
            stamped.append((-st.st_mtime_ns, p))
        ranked = [p for _, p in heapq.nsmallest(limit, stamped)]
    return {"files": [{"path": p} for p in ranked], "truncated": truncated}


# ------------------------------------------------------------------ reading


def _is_git_internal(rel: str) -> bool:
    # Case-insensitive: on case-insensitive filesystems ``.GIT`` is ``.git``.
    return any(part.lower() == ".git" for part in rel.split("/"))


def _open_beneath(root: str, rel: str) -> int:
    """Open ``root/rel`` read-only and non-blocking without following any symlink on
    the way, from ``root``'s directory fd. Raises ``FileAccessError``."""
    parts = rel.split("/")
    try:
        dfd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError as exc:
        raise FileAccessError(FILE_NOT_FOUND) from exc
    try:
        for part in parts[:-1]:
            try:
                nfd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                              dir_fd=dfd)
            except OSError as exc:
                raise _open_error(exc, part, dfd) from exc
            os.close(dfd)
            dfd = nfd
        try:
            return os.open(parts[-1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
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
    """``{path, text, binary, truncated, size}`` of a regular file beneath ``cwd`` in
    its git work tree.

    ``path`` is relative to ``cwd`` (or absolute inside it) — as the CLI resolves
    ``@path`` — and the returned ``path`` is its resolved cwd-relative form. At most
    ``MAX_READ_BYTES`` are returned (``truncated`` beyond); a NUL in the first
    ``BINARY_SNIFF_BYTES`` makes it ``binary`` with ``text: None``. ``size`` is the
    file's size on disk.

    Raises ``FileAccessError(PATH_OUTSIDE_WORKTREE)`` for anything resolving outside
    ``cwd`` (above it included), into ``.git``, or to something other than a regular file (a FIFO
    never blocks), and ``FileAccessError(FILE_NOT_FOUND)`` for a missing file or a
    missing, empty or non-git ``cwd``.
    """
    try:
        top = worktree_top(cwd)
    except GitTimeout:
        top = None
    if top is None:
        raise FileAccessError(FILE_NOT_FOUND)
    rel = worktree_rel(path, cwd) if isinstance(path, str) else None
    if rel is None or _is_git_internal(rel):
        raise FileAccessError(PATH_OUTSIDE_WORKTREE)
    # worktree_rel resolved ``rel`` against the real cwd; walk from that same root.
    fd = _open_beneath(os.path.realpath(cwd), rel)
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
    "DEFAULT_LIST_LIMIT", "FILE_NOT_FOUND", "MAX_LIST_LIMIT", "MAX_QUERY_CHARS",
    "MAX_READ_BYTES", "MAX_SCAN_ENTRIES", "PATH_OUTSIDE_WORKTREE", "SESSION_NOT_FOUND",
    "FileAccessError", "list_files", "read_file",
]
