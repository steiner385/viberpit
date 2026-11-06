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
import re
import stat
import time
from typing import Any

from server.permission_rules.patterns import worktree_rel
from viberpit.fs_beneath import SymlinkInPath, open_beneath
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

# Content search (D-20) bounds.
MAX_SEARCH_MATCHES = 200
MAX_SEARCH_FILES = 50
MAX_MATCHES_PER_FILE = 20     # ``git grep --max-count``: one noisy file cannot eat the cap
MAX_SEARCH_LINE_CHARS = 300
MIN_SEARCH_QUERY_CHARS = 2
SEARCH_TIMEOUT_S = 5.0
_SEARCH_CONTEXT_CHARS = 100   # of a long line, kept before the match
_MAX_SEARCH_LINE_BYTES = 1 << 20  # a single line past this stops the search (truncated)

PATH_OUTSIDE_WORKTREE = "PATH_OUTSIDE_WORKTREE"
FILE_NOT_FOUND = "FILE_NOT_FOUND"
BAD_REGEX = "BAD_REGEX"
# ``error_data`` code of the KeyError for an unknown session on any worktree read.
SESSION_NOT_FOUND = "SESSION_NOT_FOUND"

_clock = time.monotonic  # the ranking deadline's clock (a test seam)


class FileAccessError(Exception):
    """A file read or search refused: ``code`` is ``PATH_OUTSIDE_WORKTREE`` (400),
    ``FILE_NOT_FOUND`` (404) or, for a search, ``BAD_REGEX`` (400).

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
    the way, from ``root``'s directory fd (:mod:`viberpit.fs_beneath`). Raises
    ``FileAccessError``."""
    try:
        rfd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError as exc:
        raise FileAccessError(FILE_NOT_FOUND) from exc
    try:
        return open_beneath(rfd, rel)
    except (SymlinkInPath, ValueError) as exc:
        raise FileAccessError(PATH_OUTSIDE_WORKTREE) from exc
    except OSError as exc:
        if exc.errno in (errno.ENOENT, errno.ENOTDIR):
            raise FileAccessError(FILE_NOT_FOUND) from exc
        raise FileAccessError(PATH_OUTSIDE_WORKTREE) from exc
    finally:
        os.close(rfd)


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


# ------------------------------------------------------------------ content search


def _match_span(text: str, start: int, q: str, regex: bool, ignore_case: bool) -> int:
    """End of the match that begins at ``start`` in ``text``. A fixed string is its own
    length; a regex is re-matched with Python's ``re`` at that offset (ERE and ``re``
    differ at the edges, so a miss degrades to an empty highlight, never an error)."""
    if not regex:
        return min(start + len(q), len(text))
    try:
        m = re.compile(q, re.IGNORECASE if ignore_case else 0).match(text, start)
    except re.error:
        return start
    return m.end() if m else start


def _clip_line(text: str, start: int, end: int) -> tuple[str, int, int]:
    """``text`` cut to ``MAX_SEARCH_LINE_CHARS`` around the match, with ``start``/``end``
    re-based onto the returned text and clamped inside it."""
    if len(text) > MAX_SEARCH_LINE_CHARS:
        lo = max(0, min(start - _SEARCH_CONTEXT_CHARS, len(text) - MAX_SEARCH_LINE_CHARS))
        text = text[lo:lo + MAX_SEARCH_LINE_CHARS]
        start, end = start - lo, end - lo
    start = max(0, min(start, len(text)))
    return text, start, max(start, min(end, len(text)))


def search_files(cwd: str, q: str, regex: bool = False,
                 limit: int = MAX_SEARCH_MATCHES) -> dict[str, Any]:
    """``{matches: [{path, line, text, start, end}], truncated, timedOut}`` for ``q`` in
    the tracked files beneath ``cwd`` (D-20 Files tab content search).

    ``git grep`` over the working tree, run in ``cwd`` so paths are cwd-relative — what
    :func:`read_file` takes — and nothing above ``cwd`` is searched. The argument list
    is built here and never touches a shell; ``-e <q>`` keeps a leading ``-`` from being
    a flag. Fixed-string unless ``regex`` (then ERE); ``-i`` when ``q`` has no upper
    case (smart case). Only tracked files, so untracked and ignored ones are never
    read; git skips a symlink in the work tree rather than following it, and ``-I``
    skips binaries. The shared hardened environment (``GIT_OPTIONAL_LOCKS=0``,
    ``--no-optional-locks``) means the index is never written.

    Bounded: ``limit`` (clamped to ``MAX_SEARCH_MATCHES``) matches over at most
    ``MAX_SEARCH_FILES`` files, ``MAX_MATCHES_PER_FILE`` per file, git killed after
    ``SEARCH_TIMEOUT_S`` (``timedOut``, with what was found). ``truncated`` means a cap
    cut the results. Each ``text`` is at most ``MAX_SEARCH_LINE_CHARS`` and ``start`` /
    ``end`` are character offsets of the match in it.

    A ``q`` shorter than ``MIN_SEARCH_QUERY_CHARS`` finds nothing. Raises
    ``FileAccessError(FILE_NOT_FOUND)`` for a missing, empty or non-git ``cwd`` and
    ``FileAccessError(BAD_REGEX)`` when git rejects a ``regex`` pattern.
    """
    q = q[:MAX_QUERY_CHARS]
    limit = max(1, min(int(limit), MAX_SEARCH_MATCHES))
    try:
        top = worktree_top(cwd)
    except GitTimeout:
        top = None
    if top is None:
        raise FileAccessError(FILE_NOT_FOUND)
    if len(q) < MIN_SEARCH_QUERY_CHARS:
        return {"matches": [], "truncated": False, "timedOut": False}
    ignore_case = q == q.lower()
    args = ["-c", "grep.fullName=false", "grep", "-n", "--column", "-z", "--no-color",
            "-I", f"--max-count={MAX_MATCHES_PER_FILE}", "-E" if regex else "-F"]
    if ignore_case:
        args.append("-i")
    args += ["-e", q]

    matches: list[dict[str, Any]] = []
    files: set[str] = set()
    capped = False
    pending = b""

    def take(line: bytes) -> bool:
        nonlocal capped
        # ``-z`` makes every separator a NUL: ``path \0 lineno \0 column \0 text`` (the
        # text itself may hold anything but a newline).
        parts = line.split(b"\0", 3)
        if len(parts) != 4:
            return False
        try:
            path = parts[0].decode("utf-8")
            lineno, col = int(parts[1]), int(parts[2])
        except (UnicodeDecodeError, ValueError):
            return False  # not valid UTF-8 cannot round-trip through JSON to a read
        if path not in files:
            if len(files) >= MAX_SEARCH_FILES:
                capped = True
                return True
            files.add(path)
        raw = parts[3]
        start = len(raw[:max(col - 1, 0)].decode("utf-8", "replace"))
        text = raw.decode("utf-8", "replace")
        end = _match_span(text, start, q, regex, ignore_case)
        text, start, end = _clip_line(text, start, end)
        matches.append({"path": path, "line": lineno, "text": text,
                        "start": start, "end": end})
        if len(matches) >= limit:
            capped = True
            return True
        return False

    def consume(chunk: bytes) -> bool:
        nonlocal pending, capped
        *lines, pending = (pending + chunk).split(b"\n")
        for ln in lines:
            if take(ln):
                return True
        if len(pending) > _MAX_SEARCH_LINE_BYTES:
            capped = True
            return True
        return False

    status = stream_git(cwd, args, consume, timeout=SEARCH_TIMEOUT_S, ok_codes=(0, 1))
    if status == "failed" and regex:
        raise FileAccessError(BAD_REGEX)
    return {"matches": matches, "truncated": capped, "timedOut": status == "timeout"}


__all__ = [
    "BAD_REGEX", "DEFAULT_LIST_LIMIT", "FILE_NOT_FOUND", "MAX_LIST_LIMIT",
    "MAX_QUERY_CHARS", "MAX_READ_BYTES", "MAX_SCAN_ENTRIES", "MAX_SEARCH_MATCHES",
    "MIN_SEARCH_QUERY_CHARS", "PATH_OUTSIDE_WORKTREE", "SESSION_NOT_FOUND",
    "FileAccessError", "list_files", "read_file", "search_files",
]
