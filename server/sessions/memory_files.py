"""A session's loaded memory files (D-13): the set, and reading one of them.

The set is the CLI's own ``get_context_usage().memoryFiles`` — ``[{path, type,
tokens}]`` in load order, absolute paths, the CLI's ``type`` values — carried on the
``contextUsage`` event and kept on the session record (:func:`normalize_memory_files`).

:func:`read_memory_file` is the owning-host viewer. Its security model is an allowlist,
not a containment check alone: the CLI already sent these bytes to the model, so showing
them to the session's owner discloses nothing new — provided the request names a path
**exactly** as the set holds it. Then, because the user file is a symlink by design
(``$CLAUDE_CONFIG_DIR/CLAUDE.md`` → ``~/.claude/CLAUDE.md``), the path is resolved once
and the result must sit where the CLI loads memory from (:func:`_roots`: the work tree,
the config dirs' user file, rules and AutoMem trees, the ancestor walk names, the
managed dir); the resolved path is then opened component by component with
``O_NOFOLLOW`` (:mod:`viberpit.fs_beneath`), so anything swapped for a link after the
check is refused. A user import outside every root is read only by its literal,
symlink-free path (:func:`read_memory_file`). The whole set is never allowed wholesale.

Every refusal is ``MemoryFileError(MEMORY_FILE_UNKNOWN)`` (404), logged at INFO.

Synchronous by design — callers run it via ``asyncio.to_thread``.
"""
from __future__ import annotations

import errno
import logging
import os
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from viberpit import paths, user_memory
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
#: The CLI's managed-memory dir on Linux (``getManagedFilePath()``; a test seam).
MANAGED_DIR = "/etc/claude-code"
#: The CLI's ``type`` for the user file, its rules and its imports.
USER_TYPE = "User"
#: Project-walk names the CLI tries in every ancestor of the cwd (spike §1).
_WALK_FILES = ("CLAUDE.md", os.path.join(".claude", "CLAUDE.md"), "CLAUDE.local.md")
_WALK_RULES = os.path.join(".claude", "rules")


class MemoryFileError(Exception):
    """A memory-file read refused. ``error_data`` rides the daemon's RPC envelope, so a
    remote edge rebuilds it with its code (``RemoteOrchestrator.memory_file``), and
    marks it an expected refusal the daemon logs at INFO."""

    def __init__(self, code: str = MEMORY_FILE_UNKNOWN) -> None:
        super().__init__(code)
        self.code = code
        self.error_data = {"code": code}


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


@dataclass(frozen=True)
class _Roots:
    """Where the CLI loads memory from, every entry already resolved."""
    files: frozenset[str]           # exact files
    trees: tuple[str, ...]          # any file beneath
    automem: tuple[str, ...]        # <cfg>/projects: only <slug>/memory/** beneath


def _roots(cwd: str, config_dirs: Iterable[str]) -> _Roots:
    """The CLI's load locations (spike §1), resolved now — before the requested path.

    - the work tree (project files and their in-tree imports);
    - per config dir (the session's, plus the shared base): ``CLAUDE.md``, the
      ``rules/`` tree and the AutoMem ``projects/<slug>/memory/`` trees, each realpath'd
      because the account dir links them to the shared base by design;
    - per ancestor of the cwd, the walk names only — ``CLAUDE.md``, ``.claude/CLAUDE.md``,
      ``CLAUDE.local.md`` and the ``.claude/rules/`` tree — joined to the resolved
      ancestor WITHOUT resolving the leaf, so a walk name linked elsewhere is refused;
    - the managed dir (``/etc/claude-code``)."""
    files: set[str] = set()
    trees: list[str] = []
    automem: list[str] = []
    root = _work_root(cwd)
    if root is not None:
        trees.append(root)
    for d in {*(d for d in config_dirs if d), str(paths.shared_base())}:
        files.add(os.path.realpath(os.path.join(d, USER_MEMORY_FILENAME)))
        trees.append(os.path.realpath(os.path.join(d, "rules")))
        automem.append(os.path.realpath(os.path.join(d, "projects")))
    if cwd and os.path.isabs(cwd):
        here = os.path.realpath(cwd)
        while here != os.path.dirname(here):  # every ancestor, stopping before "/"
            files.update(os.path.join(here, n) for n in _WALK_FILES)
            trees.append(os.path.join(here, _WALK_RULES))
            here = os.path.dirname(here)
    trees.append(os.path.realpath(MANAGED_DIR))
    return _Roots(frozenset(files), tuple(trees), tuple(automem))


def _within(roots: _Roots, resolved: str) -> bool:
    if resolved in roots.files or any(_inside(t, resolved) for t in roots.trees):
        return True
    for base in roots.automem:
        if _inside(base, resolved):
            parts = os.path.relpath(resolved, base).split(os.sep)
            if len(parts) >= 3 and parts[1] == "memory":
                return True
    return False


def _is_clean_abs(path: str) -> bool:
    return os.path.isabs(path) and "\0" not in path and os.path.normpath(path) == path


def read_memory_file(path: str, *, loaded: list[dict[str, Any]] | None, cwd: str,
                     config_dirs: Iterable[str]) -> dict[str, Any]:
    """``{path, type, text, truncated, editable}`` of ``path``, a member of ``loaded``
    (the session's current set). ``cwd`` is the session's cwd; ``config_dirs`` its
    ``CLAUDE_CONFIG_DIR`` candidates. Raises :class:`MemoryFileError`.

    The chain: an exact set match; every root resolved (:func:`_roots`); the path
    resolved once and required inside a root; the resolved path opened with
    ``O_NOFOLLOW`` on every component. A **user** import outside every root (the CLI
    follows ``@~/x`` and absolute imports from the user file) is the one exception: it
    is read only through its literal path with no symlink at any component and no
    realpath, so the bytes come from exactly the file the CLI named.

    ``editable`` only for the editor's file (``user_memory.edit_path()``), and only while
    that is a regular file — the editor refuses any other symlinked one
    (``MEMORY_FILE_IS_LINK``)."""
    if not isinstance(path, str) or not _is_clean_abs(path):
        raise _refuse("not_in_set")
    entries = [e for e in loaded or () if isinstance(e, dict) and e.get("path") == path]
    if not entries:
        raise _refuse("not_in_set")
    # Every allowed root is resolved BEFORE the requested path, so a swap racing the
    # resolution can only make the O_NOFOLLOW open below refuse.
    roots = _roots(cwd, config_dirs)
    # The editor's file (``~/.claude/CLAUDE.md``, or the account-store file it links to
    # once moved — #3246): editable only while it is a regular file, as the editor needs.
    edit_path = str(user_memory.edit_path())
    try:
        shared_regular = stat.S_ISREG(os.lstat(edit_path).st_mode)
    except OSError:
        shared_regular = False
    shared = os.path.realpath(edit_path)
    resolved = os.path.realpath(path)
    if _within(roots, resolved):
        target = resolved
    elif any(e.get("type") == USER_TYPE for e in entries):
        target = path  # the literal walk below refuses any symlinked component
    else:
        raise _refuse("outside_allowed_roots")
    fd = _open_resolved(target)
    with os.fdopen(fd, "rb") as fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            raise _refuse("not_a_regular_file")
        data = fh.read(MAX_MEMORY_BYTES + 1)
    return {
        "path": path,
        "type": entries[0].get("type"),
        # Lossy decode is fine here: the viewer is read-only, and a cut at the cap can
        # split a character. The editor decodes strictly.
        "text": data[:MAX_MEMORY_BYTES].decode("utf-8", "replace"),
        "truncated": len(data) > MAX_MEMORY_BYTES,
        "editable": shared_regular and target == resolved == shared,
    }


__all__ = [
    "MANAGED_DIR", "MAX_MEMORY_BYTES", "MAX_MEMORY_ENTRIES", "MEMORY_FILE_UNKNOWN",
    "USER_MEMORY_FILENAME", "USER_TYPE",
    "MemoryFileError", "normalize_memory_files", "read_memory_file",
]
