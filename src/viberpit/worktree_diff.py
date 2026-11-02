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

Work is bounded: at most ``MAX_FILES`` files get hunks, patch output is streamed and
cut once it passes the remaining budget plus one file's cap, and untracked files are
read for line counts only up to ``MAX_TOTAL_BYTES`` in total. Whenever something is
left unread, ``truncated`` is true and the totals are **lower bounds**.

Git runs inside an agent-controlled worktree in a credential-holding daemon, so repo
config cannot run helpers here: no textconv, no external diff, no fsmonitor, no hooks,
no system config, and every ``filter.<name>`` driver defined in the repo-local or
per-worktree config scope (which the agent can write) is neutralised with empty
``clean``/``smudge``/``process`` and ``required=false``; a name that cannot be safely
spliced into ``-c`` fails closed. Submodules are never entered
(``--ignore-submodules=dirty``), so a submodule's own config cannot run one either.

Two residuals are accepted; both need the agent to write config files outside its
working files. The acceptEdits write boundary (#3217) is now enforced: auto-approved
writes stay inside the worktree and never touch ``.git`` or other protected config. The
residuals still apply to shell-capable modes (full-auto, bypass, or an approved Bash),
which have same-user access by design:

- **TOCTOU.** The overrides are computed from a config read that precedes the diff
  calls; a repo or worktree config rewritten in between (a new driver name) can still
  run a driver.
- **Global scope is trusted.** Global-scope drivers (e.g. git-lfs from
  ``git lfs install``) run when git re-hashes a stat-dirty file. That assumes nothing
  untrusted writes ``~/.gitconfig`` or the global attributes file — a same-user agent
  that can write them controls every git call the daemon makes.
"""

from __future__ import annotations

import os
import re
import selectors
import stat
import subprocess
import time
from typing import Any

MAX_FILES = 300
MAX_FILE_PATCH_BYTES = 262144
MAX_TOTAL_BYTES = 2097152

BINARY_SNIFF_BYTES = 8192
_PATCH_BATCH = 64
GIT_TIMEOUT_S = 30
# Environment that would point git at some other repository than ``cwd``'s.
_REPO_ENV_VARS = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
    "GIT_COMMON_DIR", "GIT_NAMESPACE", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
)
# Pin every config knob that could reshape the output or run repo-supplied programs.
GIT_GLOBAL = (
    "--no-optional-locks", "--literal-pathspecs",
    "-c", "core.quotePath=false", "-c", "core.fsmonitor=false",
    "-c", "core.hooksPath=/dev/null", "-c", "diff.suppressBlankEmpty=false",
)
# Explicit prefixes override diff.noprefix / mnemonicPrefix / srcPrefix / dstPrefix.
# ``--ignore-submodules=dirty`` stops git spawning ``git status`` inside each
# submodule (whose own ``.git/modules/<name>/config`` filter drivers would run here,
# out of reach of ``_filter_overrides``); pointer changes are still reported, and
# ``--submodule=short`` overrides ``diff.submodule=log|diff``, which would also read
# into the submodule.
_DIFF_OPTS = ("--no-color", "--no-ext-diff", "--no-textconv", "--no-relative", "-M",
              "--src-prefix=a/", "--dst-prefix=b/", "--ignore-submodules=dirty",
              "--submodule=short")
_HUNK_RE = re.compile(rb"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$")
# ``-M`` overrides ``diff.renames=copies``, so no copy (``C``) status ever appears.
_STATUS = {"A": "added", "D": "deleted", "R": "renamed"}


class GitTimeout(Exception):
    """A git call ran past ``GIT_TIMEOUT_S`` — distinct from failure, so a hung git
    never reads as a clean tree."""


def git_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _REPO_ENV_VARS}
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    return env


def run_git(cwd: str, *args: str) -> bytes | None:
    """Run git in ``cwd``; stdout bytes, or None on failure (raises ``GitTimeout``).
    Bytes, not text (unlike ``worktrees._run``): paths and contents need not be UTF-8."""
    try:
        r = subprocess.run(
            ["git", "-C", cwd, *GIT_GLOBAL, *args],
            capture_output=True, env=git_env(), timeout=GIT_TIMEOUT_S, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitTimeout from exc
    except OSError:
        return None
    return r.stdout if r.returncode == 0 else None


def worktree_top(cwd: str) -> str | None:
    """The top level of ``cwd``'s git work tree, or None when ``cwd`` is empty, missing
    or not inside a work tree (raises ``GitTimeout``)."""
    if not cwd or not os.path.isdir(cwd):
        return None
    out = run_git(cwd, "rev-parse", "--show-toplevel")
    return os.fsdecode(out.rstrip(b"\n")) if out else None


def _git_bounded(cwd: str, args: list[str], limit: int,
                 block_cap: int) -> tuple[bytes, str]:
    """Stream a ``git diff`` patch, reading at most ``limit + 1`` bytes.

    Returns ``(data, status)``: ``"ok"`` (git exited 0 after EOF), ``"cut"`` (output
    passed ``limit``, or the ``diff --git`` block being read passed ``block_cap``; git
    is killed and the last block of ``data`` is partial), or ``"failed"`` (timeout,
    non-zero exit, or spawn error; the last block may be partial too).
    """
    try:
        proc = subprocess.Popen(
            ["git", "-C", cwd, *GIT_GLOBAL, *args],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=git_env(),
        )
    except OSError:
        return b"", "failed"
    assert proc.stdout is not None
    buf = bytearray()
    status = "ok"
    block_start = scanned = 0
    deadline = time.monotonic() + GIT_TIMEOUT_S
    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ)
    try:
        while True:
            left = deadline - time.monotonic()
            if left <= 0 or not sel.select(left):
                status = "failed"
                break
            chunk = os.read(proc.stdout.fileno(), min(65536, limit + 1 - len(buf)))
            if not chunk:
                break
            buf += chunk
            nxt = buf.rfind(b"\ndiff --git ", max(scanned - 12, 0))
            if nxt >= 0:
                block_start = nxt + 1
            scanned = len(buf)
            if len(buf) > limit or len(buf) - block_start > block_cap:
                status = "cut"
                break
        if status == "ok":
            try:
                if proc.wait(timeout=max(deadline - time.monotonic(), 0)) != 0:
                    status = "failed"
            except subprocess.TimeoutExpired:
                status = "failed"
    finally:
        sel.close()
        if proc.poll() is None:
            proc.kill()
        proc.stdout.close()
        proc.wait()
    return bytes(buf), status


_FILTER_KEY_RE = re.compile(rb"^filter\.(.+)\.[^.]+$", re.DOTALL)
# Scopes an agent cannot write from inside its worktree. Anything else that defines a
# filter driver (local, worktree, or an unknown scope) is neutralised.
_TRUSTED_SCOPES = {b"global", b"system", b"command"}


class _UnsafeConfig(Exception):
    """A repo-scope filter driver name cannot be neutralised safely via ``-c``."""


def _filter_overrides(top: str) -> tuple[str, ...]:
    """``-c`` args neutralising every filter driver defined in an untrusted scope.

    Raises ``_UnsafeConfig`` when a driver name cannot be spliced into a ``-c`` key
    (``=``, newline, NUL) or the config cannot be read; ``GitTimeout`` on timeout."""
    try:
        r = subprocess.run(
            ["git", "-C", top, *GIT_GLOBAL, "config", "--show-scope", "--includes",
             "--name-only", "-z", "--get-regexp", r"^filter\."],
            capture_output=True, env=git_env(), timeout=GIT_TIMEOUT_S, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitTimeout from exc
    except OSError as exc:
        raise _UnsafeConfig("git config unreadable") from exc
    if r.returncode == 1:  # no filter.* keys anywhere
        return ()
    if r.returncode != 0:
        raise _UnsafeConfig("git config unreadable")
    toks = r.stdout.split(b"\0")
    names: set[bytes] = set()
    for scope, key in zip(toks[0::2], toks[1::2]):
        if scope in _TRUSTED_SCOPES:
            continue
        m = _FILTER_KEY_RE.match(key)
        if m is None:
            raise _UnsafeConfig("unparseable filter key")
        names.add(m.group(1))
    args: list[str] = []
    for raw in sorted(names):
        if any(c in raw for c in (b"=", b"\n", b"\r", b"\0")):
            raise _UnsafeConfig("unsafe filter driver name")
        name = os.fsdecode(raw)
        for var in ("clean=", "smudge=", "process=", "required=false"):
            args += ["-c", f"filter.{name}.{var}"]
    return tuple(args)


def _text(b: bytes) -> str:
    return b.decode("utf-8", "replace")


def _empty(base: str | None = None, head: str | None = None,
           truncated: bool = False) -> dict[str, Any]:
    return {"base": base, "head": head, "files": [], "additions": 0, "deletions": 0,
            "truncated": truncated}


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
        if not path:  # rename: the two paths follow as their own tokens
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
        if letter == "R":
            status[toks[i + 1]] = letter
            i += 2
        else:
            status[toks[i]] = letter
            i += 1
    return status


def _split_patch(out: bytes) -> list[tuple[bytes, bytes]]:
    """Split ``git diff`` patch output into (``diff --git`` header line, block)."""
    starts = [m.start() for m in re.finditer(rb"(?m)^diff --git ", out)]
    return [(out[s:e].split(b"\n", 1)[0], out[s:e])
            for s, e in zip(starts, [*starts[1:], len(out)])]


def _parse_hunks(block: bytes) -> list[dict[str, Any]]:
    hunks: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    old_no = new_no = old_left = new_left = 0
    for line in block.split(b"\n"):
        m = _HUNK_RE.match(line)
        if m:
            old_no, new_no = int(m.group(1)), int(m.group(3))
            old_left = int(m.group(2)) if m.group(2) is not None else 1
            new_left = int(m.group(4)) if m.group(4) is not None else 1
            cur = {"oldStart": old_no, "oldLines": old_left, "newStart": new_no,
                   "newLines": new_left, "header": _text(m.group(5)), "lines": []}
            hunks.append(cur)
            continue
        # Only lines the hunk header still owes are file lines; "\ No newline at end
        # of file" never is.
        if cur is None or line[:1] == b"\\" or (old_left <= 0 and new_left <= 0):
            continue
        # A bare empty line is an empty context line (diff.suppressBlankEmpty).
        tag, text = line[:1] or b" ", _text(line[1:])
        if tag == b" ":
            cur["lines"].append({"kind": "context", "oldNo": old_no, "newNo": new_no, "text": text})
            old_no, new_no, old_left, new_left = old_no + 1, new_no + 1, old_left - 1, new_left - 1
        elif tag == b"-":
            cur["lines"].append({"kind": "del", "oldNo": old_no, "newNo": None, "text": text})
            old_no, old_left = old_no + 1, old_left - 1
        elif tag == b"+":
            cur["lines"].append({"kind": "add", "oldNo": None, "newNo": new_no, "text": text})
            new_no, new_left = new_no + 1, new_left - 1
    return hunks


# -- untracked files -------------------------------------------------------------------

def _untracked(top: str, path: bytes, *, want_hunks: bool,
               read_left: int) -> tuple[dict[str, Any], int, int] | None:
    """An untracked file as an ``added`` FileDiff, with its synthetic patch size and
    the bytes read; None (nothing read) when it would exceed ``read_left``."""
    full = os.path.join(os.fsencode(top), path)
    try:
        st = os.lstat(full)
        if stat.S_ISLNK(st.st_mode):
            data: bytes | None = os.readlink(full)
            size = len(data or b"")
        elif stat.S_ISREG(st.st_mode):
            size = st.st_size
            data = None
        else:
            return _file(path, None, "added", 0, 0, False), 0, 0
        if size > read_left:
            return None
        if data is None:
            # The agent can swap the path after the lstat: a FIFO opened blocking would
            # park this worker thread, so open non-blocking, never through a symlink,
            # and read only what is still a regular file.
            fd = os.open(full, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                os.close(fd)
                return _file(path, None, "added", 0, 0, False), 0, 0
            with os.fdopen(fd, "rb") as fh:
                head = fh.read(BINARY_SNIFF_BYTES)
                if b"\0" in head:
                    return _file(path, None, "added", 0, 0, True), 0, len(head)
                if want_hunks and size <= MAX_FILE_PATCH_BYTES:
                    data = head + fh.read(MAX_FILE_PATCH_BYTES + 1 - len(head))
                else:  # count lines without holding the file, never past what we stat'd
                    nl, last, read = head.count(b"\n"), head[-1:], len(head)
                    while read < size and (chunk := fh.read(min(1 << 20, size - read))):
                        nl += chunk.count(b"\n")
                        last, read = chunk[-1:], read + len(chunk)
                    lines = nl + (1 if last and last != b"\n" else 0)
                    f = _file(path, None, "added", lines, 0, False)
                    f["tooLarge"] = want_hunks
                    return f, 0, read
    except OSError:
        return _file(path, None, "added", 0, 0, False), 0, 0
    assert data is not None
    if b"\0" in data[:BINARY_SNIFF_BYTES]:
        return _file(path, None, "added", 0, 0, True), 0, len(data)
    rows = data.split(b"\n")
    if rows and rows[-1] == b"":
        rows.pop()
    f = _file(path, None, "added", len(rows), 0, False)
    if not want_hunks or not rows:
        return f, 0, len(data)
    patch_size = len(data) + len(rows)
    if patch_size > MAX_FILE_PATCH_BYTES:
        f["tooLarge"] = True
        return f, 0, len(data)
    f["hunks"] = [{
        "oldStart": 0, "oldLines": 0, "newStart": 1, "newLines": len(rows), "header": "",
        "lines": [{"kind": "add", "oldNo": None, "newNo": n, "text": _text(r)}
                  for n, r in enumerate(rows, 1)],
    }]
    return f, patch_size, len(data)


# -- entry point -----------------------------------------------------------------------

def _resolve_base(top: str) -> tuple[str | None, str | None, str]:
    """(reported base, head, rev to diff from)."""
    out = run_git(top, "rev-parse", "--verify", "-q", "HEAD")
    head = _text(out).strip() if out else None
    if head is None:  # unborn branch: everything is new
        empty = run_git(top, "hash-object", "-t", "tree", os.devnull)
        return None, None, _text(empty or b"").strip()
    out = run_git(top, "symbolic-ref", "-q", "refs/remotes/origin/HEAD")
    default = _text(out).strip() if out else ""
    if default:
        out = run_git(top, "merge-base", "HEAD", default)
        if out:
            base = _text(out).strip()
            return base, head, base
    return None, head, head


def session_diff(cwd: str, *, summary: bool = False,
                 ignore_whitespace: bool = False) -> dict[str, Any]:
    """The ``SessionDiff`` (camelCase) of ``cwd`` from its base to the working tree;
    see :func:`_session_diff`. Never raises ``KeyError``: callers map that to
    "session not found", so an internal bug must not masquerade as a 404."""
    try:
        return _session_diff(cwd, summary=summary, ignore_whitespace=ignore_whitespace)
    except KeyError as exc:
        raise RuntimeError(f"session diff failed: {exc!r}") from exc


def _session_diff(cwd: str, *, summary: bool, ignore_whitespace: bool) -> dict[str, Any]:
    """The ``SessionDiff`` (camelCase) of ``cwd`` from its base to the working tree.

    A ``cwd`` that is missing or not a git work tree yields an empty diff. ``summary``
    skips hunks; ``ignore_whitespace`` is ``git diff -w``. When ``truncated`` is true,
    ``additions``/``deletions`` are lower bounds: untracked files beyond ``MAX_FILES``
    or past the read budget are listed unread with zero counts, and a git timeout
    yields ``files: []``.
    """
    base: str | None = None
    head: str | None = None
    try:
        top = worktree_top(cwd)
        if top is None:
            return _empty()
        base, head, rev = _resolve_base(top)
        if not rev:
            return _empty(base, head)
        # Every call that may hash working-tree files runs with these overrides.
        cfg = _filter_overrides(top)
        ws = ("-w",) if ignore_whitespace else ()
        numstat = run_git(top, *cfg, "diff", *_DIFF_OPTS, *ws, "--numstat", "-z", rev)
        names = run_git(top, *cfg, "diff", *_DIFF_OPTS, *ws, "--name-status", "-z", rev)
        untracked = run_git(top, *cfg, "ls-files", "--others", "--exclude-standard", "-z")
    except (GitTimeout, _UnsafeConfig):
        return _empty(base, head, truncated=True)
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
    position = {_header(*pr): i for i, pr in enumerate(to_fetch)}
    blocks: dict[bytes, bytes] = {}   # complete patch blocks not yet consumed
    partial: dict[bytes, int] = {}    # header -> bytes seen before a fetch was cut
    tried: set[bytes] = set()         # headers a fetch has started from
    files: list[dict[str, Any]] = []
    truncated = len(combined) > MAX_FILES
    budget = MAX_TOTAL_BYTES          # hunk payload handed out
    patch_left = MAX_TOTAL_BYTES      # patch bytes read across every fetch
    untracked_left = MAX_TOTAL_BYTES  # untracked bytes read for line counts
    fetching = True                   # False once a fetch failed or patch_left ran out

    def fetch_from(header: bytes) -> None:
        """Fetch the batch starting at ``header``. A block cut for exceeding the
        per-file cap is a ``tooLarge`` file and the next file fetches afresh; a fetch
        that fails, or exhausts ``patch_left``, ends fetching for this call."""
        nonlocal patch_left, fetching
        tried.add(header)
        i = position[header]
        specs = sorted({os.fsdecode(p) for pr in to_fetch[i:i + _PATCH_BATCH] for p in pr})
        data, status = _git_bounded(
            top, [*cfg, "diff", *_DIFF_OPTS, *ws, "-U3", rev, "--", *specs],
            patch_left, MAX_FILE_PATCH_BYTES)
        patch_left -= len(data)
        parts = _split_patch(data)
        if status != "ok" and parts:
            cut_header, cut_block = parts.pop()
            partial[cut_header] = len(cut_block)
        blocks.update(parts)
        if status == "failed" or patch_left <= 0:
            fetching = False

    for idx, (path, entry, pair) in enumerate(combined):
        want = not summary and idx < MAX_FILES and budget >= 0
        if entry is None:
            got = (_untracked(top, path, want_hunks=want, read_left=untracked_left)
                   if idx < MAX_FILES else None)
            if got is None:  # listed unread: counts unknown
                files.append(_file(path, None, "added", 0, 0, False))
                truncated = True
                continue
            f, size, consumed = got
            untracked_left -= consumed
            if size > budget:
                f["hunks"], truncated, budget = [], True, -1
            else:
                budget -= size
            files.append(f)
            continue
        f = entry
        if want and pair is not None and not f["binary"]:
            header = _header(*pair)
            if oversized(f):
                f["tooLarge"] = True
            else:
                if (fetching and header not in blocks and header not in partial
                        and header not in tried and header in position):
                    fetch_from(header)
                cut = partial.pop(header, None)
                block = blocks.pop(header, None)
                if (cut or 0) > MAX_FILE_PATCH_BYTES or len(block or b"") > MAX_FILE_PATCH_BYTES:
                    f["tooLarge"] = True
                elif block is None:
                    # Never fetched whole: the fetch failed, the read budget ran out,
                    # or it completed without this file's block (an unmapped header).
                    truncated, budget = True, -1
                elif block is not None and len(block) > budget:
                    truncated, budget = True, -1
                elif block is not None:
                    budget -= len(block)
                    f["hunks"] = _parse_hunks(block)
        files.append(f)
    return {
        "base": base, "head": head, "files": files,
        "additions": sum(f["additions"] for f in files),
        "deletions": sum(f["deletions"] for f in files),
        "truncated": truncated,
    }
