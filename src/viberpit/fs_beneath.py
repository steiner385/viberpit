"""Open and create paths beneath a directory fd without ever following a symlink.

The one implementation behind every daemon-side file access into an agent-controlled
tree (D-7's Files tab read, D-8's fork copy). A path is walked component by
component from a root directory fd: each directory with ``O_DIRECTORY|O_NOFOLLOW``,
the leaf with ``O_NOFOLLOW`` (and ``O_NONBLOCK`` for reads, so a FIFO swapped in can
never park the caller). A directory swapped for a symlink after any earlier check is
refused, not followed — unlike a ``realpath`` check, there is no window between the
check and the use.

Callers validate the relative path's shape first (no ``..``, not absolute); a ``..``
component would still be refused here only if it were a symlink, which it is not.
"""
from __future__ import annotations

import errno
import os
import stat

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_READ_FLAGS = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC


class SymlinkInPath(OSError):
    """A component of the path is a symlink, so the walk refused it."""


def _split(rel: str | bytes) -> list[str] | list[bytes]:
    if isinstance(rel, bytes):
        return [p for p in rel.split(b"/") if p]
    return [p for p in rel.split("/") if p]


def _refused(exc: OSError, name: str | bytes, dfd: int) -> OSError:
    """``exc`` as :class:`SymlinkInPath` when ``name`` (in ``dfd``) is a symlink —
    ``O_NOFOLLOW`` reports one as ELOOP, or ENOTDIR with ``O_DIRECTORY``."""
    if exc.errno in (errno.ELOOP, errno.ENOTDIR):
        try:
            is_link = stat.S_ISLNK(os.lstat(name, dir_fd=dfd).st_mode)
        except OSError:
            is_link = False
        if is_link:
            return SymlinkInPath(exc.errno, f"symlink in path: {os.fsdecode(name)!r}")
    return exc


def open_dir_beneath(root_fd: int, rel: str | bytes, *, create: bool = False,
                     mode: int = 0o777) -> int:
    """A new fd for the directory ``rel`` beneath ``root_fd`` (``""`` = the root).

    With ``create``, missing components are made with ``mkdir(dir_fd=…)`` — which
    never follows a symlink either — before each is opened. Raises ``OSError``
    (``SymlinkInPath`` for a symlinked component)."""
    dfd = os.dup(root_fd)
    try:
        for part in _split(rel):
            if create:
                try:
                    os.mkdir(part, mode, dir_fd=dfd)
                except FileExistsError:
                    pass
            try:
                nfd = os.open(part, _DIR_FLAGS, dir_fd=dfd)
            except OSError as exc:
                raise _refused(exc, part, dfd) from exc
            os.close(dfd)
            dfd = nfd
    except BaseException:
        os.close(dfd)
        raise
    return dfd


def open_beneath(root_fd: int, rel: str | bytes) -> int:
    """Open the file ``rel`` beneath ``root_fd`` read-only and non-blocking, following
    no symlink on the way or at the leaf. The caller ``fstat``s what it got. Raises
    ``OSError`` (``SymlinkInPath`` for a symlinked component or leaf)."""
    parts = _split(rel)
    if not parts:
        raise IsADirectoryError(errno.EISDIR, "empty path")
    dfd = open_dir_beneath(root_fd, os.path.dirname(rel))
    try:
        return os.open(parts[-1], _READ_FLAGS, dir_fd=dfd)
    except OSError as exc:
        raise _refused(exc, parts[-1], dfd) from exc
    finally:
        os.close(dfd)
