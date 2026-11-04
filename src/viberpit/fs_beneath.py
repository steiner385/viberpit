"""Open and create paths beneath a directory fd without ever following a symlink.

The one implementation behind every daemon-side file access into an agent-controlled
tree (D-7's Files tab read, D-8's fork copy, D-9's rewind undo backup). A path is
walked component by component from a root directory fd: each directory with
``O_DIRECTORY|O_NOFOLLOW``, the leaf with ``O_NOFOLLOW`` (and ``O_NONBLOCK`` for
reads, so a FIFO swapped in can never park the caller). A directory swapped for a symlink after any earlier check is
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
    """``rel``'s components; ``ValueError`` if it is empty, absolute or has an empty
    component (``a//b``, a trailing ``/``)."""
    parts = rel.split(b"/") if isinstance(rel, bytes) else rel.split("/")
    if not all(parts):
        raise ValueError(f"not a clean relative path: {rel!r}")
    return parts


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


def open_dir_beneath(root_fd: int, rel: str | bytes | list[str] | list[bytes], *,
                     create: bool = False, mode: int = 0o777) -> int:
    """A new fd for the directory ``rel`` (a relative path, or its components) beneath
    ``root_fd``; ``""`` or ``[]`` is the root itself.

    With ``create``, missing components are made with ``mkdir(dir_fd=…)`` — which
    never follows a symlink either — before each is opened. Raises ``OSError``
    (``SymlinkInPath`` for a symlinked component); ``ValueError`` for a bad path."""
    parts = rel if isinstance(rel, list) else (_split(rel) if rel else [])
    dfd = os.dup(root_fd)
    try:
        for part in parts:
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
    ``OSError`` (``SymlinkInPath`` for a symlinked component or leaf); ``ValueError``
    for an absolute path or one with an empty component."""
    parts = _split(rel)
    dfd = open_dir_beneath(root_fd, parts[:-1])
    try:
        return os.open(parts[-1], _READ_FLAGS, dir_fd=dfd)
    except OSError as exc:
        raise _refused(exc, parts[-1], dfd) from exc
    finally:
        os.close(dfd)


def replace_file_beneath(root_fd: int, rel: str, data: bytes, mode: int, *,
                         fsync: bool = False) -> None:
    """Atomically make ``rel`` beneath ``root_fd`` a regular file holding ``data`` with
    permission bits ``mode``, following no symlink.

    Missing parent directories are made one component at a time (``open_dir_beneath``
    with ``create``), so a symlinked component raises ``SymlinkInPath`` before anything
    is written. The bytes go to a fresh ``O_EXCL|O_NOFOLLOW`` temp file in the parent,
    which is then renamed over the leaf: a symlink at the leaf is replaced, never
    written through. A directory at the leaf raises (``IsADirectoryError``). Any
    failure before the rename removes the temp file and leaves the leaf as it was.

    ``fsync`` makes it durable: the temp file is fsynced before the rename and the
    parent directory after it (the D-13 user-memory editor; a bulk fork copy skips it)."""
    parts = _split(rel)
    name = os.fsdecode(parts[-1])
    pfd = open_dir_beneath(root_fd, parts[:-1], create=True)
    try:
        tmp = f".{name}.vp-{os.urandom(6).hex()}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=pfd)
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(data)
                os.fchmod(out.fileno(), stat.S_IMODE(mode))
                if fsync:
                    out.flush()
                    os.fsync(out.fileno())
            os.replace(tmp, name, src_dir_fd=pfd, dst_dir_fd=pfd)
            if fsync:
                os.fsync(pfd)
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=pfd)
            except OSError:
                pass
            raise
    finally:
        os.close(pfd)


def unlink_file_beneath(root_fd: int, rel: str) -> bool:
    """Remove the regular file ``rel`` beneath ``root_fd``, following no symlink on the
    way. Only a regular file is removed: a missing path, or a symlink, directory or
    other non-regular leaf, is left alone. Returns whether a file was removed. Raises
    ``SymlinkInPath`` for a symlinked parent component."""
    parts = _split(rel)
    try:
        pfd = open_dir_beneath(root_fd, parts[:-1])
    except FileNotFoundError:
        return False
    try:
        try:
            st = os.lstat(parts[-1], dir_fd=pfd)
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(st.st_mode):
            return False
        os.unlink(parts[-1], dir_fd=pfd)
        return True
    finally:
        os.close(pfd)
