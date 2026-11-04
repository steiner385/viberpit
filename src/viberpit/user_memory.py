"""Where the operator's user-level ``CLAUDE.md`` really lives (D-13, #3246).

Every account's ``$CLAUDE_CONFIG_DIR/CLAUDE.md`` links to ``~/.claude/CLAUDE.md``
(:data:`viberpit.configdir.SHARED_USER_FILES`), and a session whose cwd is under HOME also
reaches that name through the CLI's project walk. A worker host (rocky) sees ``~/.claude``
over sshfs with a 600 s attribute/entry cache, so after an edit there that grows the file —
the Settings editor, or any editor that saves by temp file + rename — rocky keeps the old
size and serves a **truncated** body for up to 10 minutes: the #1005 pattern.

The fix is to keep the bytes in the account store, which every host mounts coherently
(``deploy/bin-viberpit-mounts.sh`` ``COHERENT_OPTS``), and make ``~/.claude/CLAUDE.md`` a
symlink to it. Both read paths still name ``~/.claude/CLAUDE.md``; the link itself never
changes, so caching it is harmless, and its absolute target resolves on each host to that
host's coherent mount of the store.

The move is an explicit, one-time operator step (``viberpit memory move``), never done
implicitly: ``~/.claude`` is the operator's dotfiles repository, and the move turns a
tracked file into a tracked symlink. Until it runs, ``~/.claude/CLAUDE.md`` stays the
canonical file and everything works as before. :func:`edit_path` is the one answer to
"which file do I write" for both states — the Settings editor, the session memory viewer
and config snapshot restore all route through it.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

from . import paths
from .fs_beneath import replace_file_beneath

FILENAME = "CLAUDE.md"
#: The coherently-mounted directory under the account store that holds the moved file.
STORE_SUBDIR = "shared"

MOVED = "moved"            # the file was copied into the store and replaced by the link
LINKED = "linked"          # no file existed: only the link was made (the CLI reads a
                           # dangling user-memory link as absent; the editor creates it)
ALREADY = "already"        # ~/.claude/CLAUDE.md already links to the store file


class MoveRefused(Exception):
    """The move would overwrite or lose something; nothing was changed."""


def home_path() -> Path:
    """``~/.claude/CLAUDE.md``: the name the CLI and every account link read."""
    return paths.shared_base() / FILENAME


def store_path() -> Path:
    """The coherently-mounted home of the bytes once moved."""
    return paths.root() / STORE_SUBDIR / FILENAME


def is_moved() -> bool:
    """True when ``~/.claude/CLAUDE.md`` is exactly the link to :func:`store_path`."""
    try:
        return os.readlink(home_path()) == str(store_path())
    except OSError:
        return False


def edit_path() -> Path:
    """The regular file a writer replaces: the store file once moved, else the home file.

    A home file that is any OTHER symlink is still returned as the home path, so callers
    that refuse to write through links keep refusing it."""
    return store_path() if is_moved() else home_path()


def _fsync_dir(d: Path) -> None:
    fd = os.open(d, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def move_to_store() -> str:
    """Move ``~/.claude/CLAUDE.md`` into the store and link it back. Idempotent.

    Returns :data:`MOVED`, :data:`LINKED` or :data:`ALREADY`. Raises :class:`MoveRefused`
    with nothing changed when the home file is another symlink or not a regular file,
    when the store already holds DIFFERENT bytes, or when the home file changed while it
    was being copied. Run it on the host that owns ``~/.claude`` (dobby), not through a
    worker's sshfs view of it.
    """
    home, store = home_path(), store_path()
    if is_moved():
        return ALREADY
    try:
        st: os.stat_result | None = os.lstat(home)
    except FileNotFoundError:
        st = None
    if st is not None and not stat.S_ISREG(st.st_mode):
        raise MoveRefused(f"{home} is not a regular file (a link elsewhere?) — left alone")
    data = home.read_bytes() if st is not None else None

    store.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    created = False
    if os.path.lexists(store):
        if not store.is_file() or store.is_symlink():
            raise MoveRefused(f"{store} exists and is not a regular file — left alone")
        if data is not None and store.read_bytes() != data:
            raise MoveRefused(f"{store} already exists with different contents than {home} — "
                              "reconcile them by hand, then re-run")
    elif data is not None:
        assert st is not None
        dfd = os.open(store.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            replace_file_beneath(dfd, FILENAME, data, stat.S_IMODE(st.st_mode), fsync=True)
        finally:
            os.close(dfd)
        created = True

    # An edit that landed during the copy must not be lost to the link replacing it.
    if data is not None and home.read_bytes() != data:
        if created:
            store.unlink()
        raise MoveRefused(f"{home} changed while it was being moved — nothing changed, re-run")

    tmp = home.parent / f".{FILENAME}.vp-link-{os.urandom(6).hex()}"
    os.symlink(str(store), tmp)
    try:
        os.replace(tmp, home)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    _fsync_dir(home.parent)
    return MOVED if data is not None else LINKED


__all__ = ["ALREADY", "FILENAME", "LINKED", "MOVED", "STORE_SUBDIR", "MoveRefused",
           "edit_path", "home_path", "is_moved", "move_to_store", "store_path"]
