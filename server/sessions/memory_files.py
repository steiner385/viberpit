"""A session's loaded memory files (D-13): the set, and reading one of them.

The set is the CLI's own ``get_context_usage().memoryFiles`` — ``[{path, type,
tokens}]`` in load order, absolute paths, the CLI's ``type`` values — carried on the
``contextUsage`` event and kept on the session record (:func:`normalize_memory_files`).

:func:`read_memory_file` is the owning-host viewer. Its security model is an allowlist,
not a containment check alone: the CLI already sent these bytes to the model, so showing
them to the session's owner discloses nothing new — provided the request names a path
**exactly** as the set holds it. Then, because the user file is a symlink by design
(``$CLAUDE_CONFIG_DIR/CLAUDE.md`` → ``~/.claude/CLAUDE.md``), the path is resolved once
and the result must sit inside the session's work tree or be one of the user files; the
resolved path is then opened component by component with ``O_NOFOLLOW``
(:mod:`viberpit.fs_beneath`), so anything swapped for a link after the check is refused.

Every refusal is ``MemoryFileError(MEMORY_FILE_UNKNOWN)`` (404), logged at INFO.

Synchronous by design — callers run it via ``asyncio.to_thread``.
"""
from __future__ import annotations

import errno
import logging
import os
import stat
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from viberpit import paths
from viberpit.fs_beneath import SymlinkInPath, open_beneath
from viberpit.worktree_diff import GitTimeout, worktree_top

log = logging.getLogger(__name__)

#: Viewer read cap and editor write limit, in bytes.
MAX_MEMORY_BYTES = 256 * 1024
#: Defensive bound on a stored set (the CLI reports a few dozen at most).
MAX_MEMORY_ENTRIES = 200
#: The user-level memory file's name, in ``$CLAUDE_CONFIG_DIR`` and the shared base.
USER_MEMORY_FILENAME = "CLAUDE.md"
MEMORY_FILE_UNKNOWN = "MEMORY_FILE_UNKNOWN"


class MemoryFileError(Exception):
    """A memory-file read refused. ``error_data`` rides the daemon's RPC envelope, so a
    remote edge rebuilds it with its code (``RemoteOrchestrator.memory_file``), and
    marks it an expected refusal the daemon logs at INFO."""

    def __init__(self, code: str = MEMORY_FILE_UNKNOWN) -> None:
        super().__init__(code)
        self.code = code
        self.error_data = {"code": code}


def shared_user_memory_path() -> Path:
    """The one shared user memory file every account links to (D-13 Task 1)."""
    return paths.shared_base() / USER_MEMORY_FILENAME


def normalize_memory_files(raw: object) -> list[dict[str, Any]] | None:
    """``memoryFiles`` from a ``get_context_usage`` reply as ``[{path, type, tokens}]``,
    CLI order kept, rows without a string path and type dropped. None when the reply
    has no list (not reported — keep whatever was stored), ``[]`` when it is empty."""
    if not isinstance(raw, list):
        return None
    out: list[dict[str, Any]] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        path, kind, tokens = row.get("path"), row.get("type"), row.get("tokens")
        if not isinstance(path, str) or not path or not isinstance(kind, str):
            continue
        ok = isinstance(tokens, (int, float)) and not isinstance(tokens, bool)
        out.append({"path": path, "type": kind, "tokens": int(tokens) if ok else 0})  # type: ignore[arg-type]
        if len(out) >= MAX_MEMORY_ENTRIES:
            break
    return out


def _refuse(why: str) -> MemoryFileError:
    log.info("memory_file.refused reason=%s", why)
    return MemoryFileError(MEMORY_FILE_UNKNOWN)


def _inside(root: str, path: str) -> bool:
    try:
        return os.path.commonpath([root, path]) == root
    except ValueError:
        return False


def _work_root(cwd: str) -> str | None:
    """The resolved work-tree top of ``cwd`` (the CLI walks up to it, so the repo-root
    CLAUDE.md of a session started in a subdirectory is "inside"), else ``cwd``."""
    if not cwd or not os.path.isdir(cwd):
        return None
    try:
        top = worktree_top(cwd)
    except GitTimeout:
        top = None
    return os.path.realpath(top or cwd)


def _open_resolved(resolved: str) -> int:
    """Open the already-resolved absolute ``resolved`` from ``/`` following no symlink
    anywhere on the way."""
    rfd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        return open_beneath(rfd, resolved.lstrip("/"))
    except (SymlinkInPath, ValueError) as exc:
        raise _refuse("symlink_after_resolve") from exc
    except OSError as exc:
        raise _refuse("missing" if exc.errno in (errno.ENOENT, errno.ENOTDIR)
                      else "unreadable") from exc
    finally:
        os.close(rfd)


def read_memory_file(path: str, *, loaded: list[dict[str, Any]] | None, cwd: str,
                     config_dirs: Iterable[str]) -> dict[str, Any]:
    """``{path, type, text, truncated, editable}`` of ``path``, a member of ``loaded``
    (the session's current set). ``cwd`` is the session's cwd; ``config_dirs`` its
    ``CLAUDE_CONFIG_DIR`` candidates. ``editable`` only for the shared user file —
    the one the Settings editor writes. Raises :class:`MemoryFileError`."""
    entry = next((e for e in loaded or () if isinstance(e, dict) and e.get("path") == path),
                 None)
    if entry is None or not isinstance(path, str) or not os.path.isabs(path) or "\0" in path:
        raise _refuse("not_in_set")
    # Every allowed root is resolved BEFORE the requested path, so a swap racing the
    # resolution can only make the O_NOFOLLOW open below refuse.
    root = _work_root(cwd)
    shared = os.path.realpath(shared_user_memory_path())
    user_files = {shared} | {os.path.realpath(os.path.join(d, USER_MEMORY_FILENAME))
                             for d in config_dirs if d}
    resolved = os.path.realpath(path)
    if resolved not in user_files and (root is None or not _inside(root, resolved)):
        raise _refuse("outside_allowed_roots")
    fd = _open_resolved(resolved)
    with os.fdopen(fd, "rb") as fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            raise _refuse("not_a_regular_file")
        data = fh.read(MAX_MEMORY_BYTES + 1)
    return {
        "path": path,
        "type": entry.get("type"),
        "text": data[:MAX_MEMORY_BYTES].decode("utf-8", "replace"),
        "truncated": len(data) > MAX_MEMORY_BYTES,
        "editable": resolved == shared,
    }


__all__ = [
    "MAX_MEMORY_BYTES", "MAX_MEMORY_ENTRIES", "MEMORY_FILE_UNKNOWN", "USER_MEMORY_FILENAME",
    "MemoryFileError", "normalize_memory_files", "read_memory_file",
    "shared_user_memory_path",
]
