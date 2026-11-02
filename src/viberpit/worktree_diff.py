"""Structured diff of a session's worktree against its base — the data behind the
Changes pane (D-4).

The base is ``git merge-base HEAD <origin default>``, the origin default being the
local ``refs/remotes/origin/HEAD`` (as in :func:`worktrees.commits_ahead_of_default`:
local refs only, never the network). Without a recorded ``origin/HEAD`` the base is
``HEAD`` itself, so only uncommitted work shows and ``base`` is reported as ``None``.

The diff runs from the base to the **working tree**: committed and uncommitted
tracked changes, plus untracked non-ignored files reported as ``added`` with hunks
synthesised from their content. Strictly read-only: ``--no-optional-locks``, no
``git add -N``, nothing that writes the index.

Synchronous and side-effect free by design — callers run it via ``asyncio.to_thread``.
"""

from __future__ import annotations

import os
import re
import subprocess
from typing import Any

MAX_FILES = 300
MAX_FILE_PATCH_BYTES = 262144
MAX_TOTAL_BYTES = 2097152

_BINARY_SNIFF_BYTES = 8192
_PATCH_BATCH = 64
_GIT_TIMEOUT_S = 30
# Environment that would point git at some other repository than ``cwd``'s.
_REPO_ENV_VARS = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
    "GIT_COMMON_DIR", "GIT_NAMESPACE", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
)
# Pin every config knob that could reshape the output or run repo-supplied programs.
_GIT_GLOBAL = (
    "--no-optional-locks", "--literal-pathspecs",
    "-c", "core.quotePath=false", "-c", "core.fsmonitor=false",
    "-c", "diff.noprefix=false", "-c", "diff.mnemonicPrefix=false",
)
_DIFF_OPTS = ("--no-color", "--no-ext-diff", "--no-textconv", "--no-relative", "-M")
_HUNK_RE = re.compile(rb"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$")
_STATUS = {"A": "added", "D": "deleted", "R": "renamed", "C": "added"}


def _git(cwd: str, *args: str) -> bytes | None:
    """Run git in ``cwd``; stdout bytes, or None on any failure. Bytes, not text
    (unlike ``worktrees._run``): paths and file contents need not be UTF-8."""
    env = {k: v for k, v in os.environ.items() if k not in _REPO_ENV_VARS}
    env["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        r = subprocess.run(
            ["git", "-C", cwd, *_GIT_GLOBAL, *args],
            capture_output=True, env=env, timeout=_GIT_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def _text(b: bytes) -> str:
    return b.decode("utf-8", "replace")


def _empty(base: str | None = None, head: str | None = None) -> dict[str, Any]:
    return {"base": base, "head": head, "files": [], "additions": 0, "deletions": 0,
            "truncated": False}


def _file(path: bytes, old: bytes | None, status: str, adds: int, dels: int,
          binary: bool) -> dict[str, Any]:
    return {"path": _text(path), "oldPath": _text(old) if old is not None else None,
            "status": status, "additions": adds, "deletions": dels, "binary": binary,
            "tooLarge": False, "hunks": []}


# -- git's C-style path quoting, to map ``diff --git`` headers back to files ----------

_CQ_NAMED = {0x07: b"\\a", 0x08: b"\\b", 0x09: b"\\t", 0x0A: b"\\n", 0x0B: b"\\v",
             0x0C: b"\\f", 0x0D: b"\\r", 0x22: b'\\"', 0x5C: b"\\\\"}


def _quote(name: bytes) -> bytes:
    """``quote_c_style`` with ``core.quotePath=false``: high bytes stay raw."""
    if not any(c < 0x20 or c in (0x22, 0x5C, 0x7F) for c in name):
        return name
    out = bytearray(b'"')
    for c in name:
        if c in _CQ_NAMED:
            out += _CQ_NAMED[c]
        elif c < 0x20 or c == 0x7F:
            out += b"\\%03o" % c
        else:
            out.append(c)
    return bytes(out + b'"')


def _header(old: bytes, new: bytes) -> bytes:
    return b"diff --git " + _quote(b"a/" + old) + b" " + _quote(b"b/" + new)


# -- parsing ---------------------------------------------------------------------------

def _parse_numstat(out: bytes) -> list[tuple[bytes, bytes | None, int | None, int | None]]:
    """``--numstat -z`` → (path, old_path, adds, dels); counts None for binary."""
    toks = out.split(b"\0")
    rows = []
    i = 0
    while i < len(toks):
        tok = toks[i]
        i += 1
        if not tok:
            continue
        adds, dels, path = tok.split(b"\t", 2)
        old = None
        if not path:  # rename/copy: the two paths follow as their own tokens
            old, path = toks[i], toks[i + 1]
            i += 2
        a = None if adds == b"-" else int(adds)
        d = None if dels == b"-" else int(dels)
        rows.append((path, old, a, d))
    return rows


def _parse_name_status(out: bytes) -> dict[bytes, str]:
    """``--name-status -z`` → new path → status letter."""
    toks = out.split(b"\0")
    status: dict[bytes, str] = {}
    i = 0
    while i < len(toks):
        tok = toks[i]
        i += 1
        if not tok:
            continue
        letter = chr(tok[0])
        if letter in "RC":
            status[toks[i + 1]] = letter
            i += 2
        else:
            status[toks[i]] = letter
            i += 1
    return status


def _split_patch(out: bytes) -> dict[bytes, bytes]:
    """Split ``git diff`` patch output into ``diff --git`` header line → block."""
    blocks: dict[bytes, bytes] = {}
    starts = [m.start() for m in re.finditer(rb"(?m)^diff --git ", out)]
    for s, e in zip(starts, [*starts[1:], len(out)]):
        block = out[s:e]
        blocks[block.split(b"\n", 1)[0]] = block
    return blocks


def _parse_hunks(block: bytes) -> list[dict[str, Any]]:
    hunks: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    old_no = new_no = 0
    for line in block.split(b"\n"):
        m = _HUNK_RE.match(line)
        if m:
            old_start, new_start = int(m.group(1)), int(m.group(3))
            cur = {
                "oldStart": old_start,
                "oldLines": int(m.group(2)) if m.group(2) is not None else 1,
                "newStart": new_start,
                "newLines": int(m.group(4)) if m.group(4) is not None else 1,
                "header": _text(m.group(5)),
                "lines": [],
            }
            hunks.append(cur)
            old_no, new_no = old_start, new_start
            continue
        if cur is None or not line:
            continue
        tag, text = line[:1], _text(line[1:])
        if tag == b" ":
            cur["lines"].append({"kind": "context", "oldNo": old_no, "newNo": new_no, "text": text})
            old_no += 1
            new_no += 1
        elif tag == b"-":
            cur["lines"].append({"kind": "del", "oldNo": old_no, "newNo": None, "text": text})
            old_no += 1
        elif tag == b"+":
            cur["lines"].append({"kind": "add", "oldNo": None, "newNo": new_no, "text": text})
            new_no += 1
        # "\ No newline at end of file" and anything else: not a line of the file.
    return hunks


# -- untracked files -------------------------------------------------------------------

def _untracked(top: str, path: bytes, *, want_hunks: bool) -> tuple[dict[str, Any], int]:
    """An untracked file as an ``added`` FileDiff, plus its synthetic patch size."""
    full = os.path.join(os.fsencode(top), path)
    try:
        st = os.lstat(full)
        if os.path.islink(full):
            data: bytes | None = os.readlink(full)
            size = len(data or b"")
        elif os.path.isfile(full):
            size = st.st_size
            data = None
        else:
            return _file(path, None, "added", 0, 0, False), 0
        if data is None:
            with open(full, "rb") as fh:
                head = fh.read(_BINARY_SNIFF_BYTES)
                if b"\0" in head:
                    return _file(path, None, "added", 0, 0, True), 0
                if want_hunks and size <= MAX_FILE_PATCH_BYTES:
                    data = head + fh.read()
                else:  # count lines without holding the file
                    nl, last = head.count(b"\n"), head[-1:]
                    while chunk := fh.read(1 << 20):
                        nl += chunk.count(b"\n")
                        last = chunk[-1:]
                    lines = nl + (1 if last and last != b"\n" else 0)
                    f = _file(path, None, "added", lines, 0, False)
                    f["tooLarge"] = want_hunks
                    return f, 0
    except OSError:
        return _file(path, None, "added", 0, 0, False), 0
    assert data is not None
    if b"\0" in data[:_BINARY_SNIFF_BYTES]:
        return _file(path, None, "added", 0, 0, True), 0
    rows = data.split(b"\n")
    if rows and rows[-1] == b"":
        rows.pop()
    f = _file(path, None, "added", len(rows), 0, False)
    if not want_hunks or not rows:
        return f, 0
    patch_size = len(data) + len(rows)
    if patch_size > MAX_FILE_PATCH_BYTES:
        f["tooLarge"] = True
        return f, 0
    f["hunks"] = [{
        "oldStart": 0, "oldLines": 0, "newStart": 1, "newLines": len(rows), "header": "",
        "lines": [{"kind": "add", "oldNo": None, "newNo": n, "text": _text(r)}
                  for n, r in enumerate(rows, 1)],
    }]
    return f, patch_size


# -- entry point -----------------------------------------------------------------------

def _resolve_base(top: str) -> tuple[str | None, str | None, str]:
    """(reported base, head, rev to diff from)."""
    out = _git(top, "rev-parse", "--verify", "-q", "HEAD")
    head = _text(out).strip() if out else None
    if head is None:  # unborn branch: everything is new
        empty = _git(top, "hash-object", "-t", "tree", os.devnull)
        return None, None, _text(empty or b"").strip()
    out = _git(top, "symbolic-ref", "-q", "refs/remotes/origin/HEAD")
    default = _text(out).strip() if out else ""
    if default:
        out = _git(top, "merge-base", "HEAD", default)
        if out:
            base = _text(out).strip()
            return base, head, base
    return None, head, head


def session_diff(cwd: str, *, summary: bool = False,
                 ignore_whitespace: bool = False) -> dict[str, Any]:
    """The ``SessionDiff`` (camelCase) of ``cwd`` from its base to the working tree.

    A ``cwd`` that is missing or not a git work tree yields an empty diff. ``summary``
    skips hunks (totals unchanged); ``ignore_whitespace`` is ``git diff -w``.
    """
    if not cwd or not os.path.isdir(cwd):
        return _empty()
    out = _git(cwd, "rev-parse", "--show-toplevel")
    if not out:
        return _empty()
    top = os.fsdecode(out.rstrip(b"\n"))
    base, head, rev = _resolve_base(top)
    if not rev:
        return _empty(base, head)
    ws = ("-w",) if ignore_whitespace else ()
    numstat = _git(top, "diff", *_DIFF_OPTS, *ws, "--numstat", "-z", rev)
    names = _git(top, "diff", *_DIFF_OPTS, *ws, "--name-status", "-z", rev)
    untracked = _git(top, "ls-files", "--others", "--exclude-standard", "-z")
    if numstat is None or names is None:
        return _empty(base, head)
    status = _parse_name_status(names)

    tracked: list[tuple[bytes, dict[str, Any] | None, tuple[bytes, bytes] | None]] = []
    for path, old, a, d in _parse_numstat(numstat):
        letter = status.get(path, "M")
        tf = _file(path, old, _STATUS.get(letter, "modified"), a or 0, d or 0,
                   a is None or d is None)
        tracked.append((path, tf, (old if old is not None else path, path)))
    untracked_paths = [p for p in (untracked or b"").split(b"\0") if p and not p.endswith(b"/")]
    # (sort key, FileDiff — None until an untracked file is read, tracked (old, new) or None)
    combined = sorted([*tracked, *((p, None, None) for p in untracked_paths)],
                      key=lambda e: e[0])

    def oversized(f: dict[str, Any]) -> bool:  # every patch line is at least 2 bytes
        return (f["additions"] + f["deletions"]) * 2 > MAX_FILE_PATCH_BYTES

    # Tracked files whose patch will be fetched, in output order, for lazy batching.
    to_fetch = [pr for _, e, pr in combined[:MAX_FILES]
                if e is not None and pr is not None and not e["binary"] and not oversized(e)]
    fetched = 0
    blocks: dict[bytes, bytes] = {}
    files: list[dict[str, Any]] = []
    truncated = len(combined) > MAX_FILES
    budget = MAX_TOTAL_BYTES
    for idx, (path, entry, pair) in enumerate(combined):
        want = not summary and idx < MAX_FILES and budget >= 0
        if entry is not None:
            f = entry
        else:
            f, size = _untracked(top, path, want_hunks=want)
            if size > budget:
                f["hunks"], truncated, budget = [], True, -1
            else:
                budget -= size
        if entry is not None and want and pair is not None and not f["binary"]:
            if oversized(f):
                f["tooLarge"] = True
            else:
                header = _header(*pair)
                if header not in blocks and fetched < len(to_fetch):
                    batch = to_fetch[fetched:fetched + _PATCH_BATCH]
                    fetched += len(batch)
                    specs = sorted({os.fsdecode(p) for pr in batch for p in pr})
                    patch = _git(top, "diff", *_DIFF_OPTS, *ws, "-U3", rev, "--", *specs)
                    blocks.update(_split_patch(patch or b""))
                block = blocks.pop(header, b"")
                if len(block) > MAX_FILE_PATCH_BYTES:
                    f["tooLarge"] = True
                elif len(block) > budget:
                    truncated, budget = True, -1
                else:
                    budget -= len(block)
                    f["hunks"] = _parse_hunks(block)
        files.append(f)
    return {
        "base": base, "head": head, "files": files,
        "additions": sum(f["additions"] for f in files),
        "deletions": sum(f["deletions"] for f in files),
        "truncated": truncated,
    }
