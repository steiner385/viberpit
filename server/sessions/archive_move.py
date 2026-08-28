"""Cross-device-safe restore of legacy archived session worktrees.

@deprecated — archiving is now a pure DB status flip that leaves the worktree
in place, so nothing moves *into* an archive home anymore. This module only
restores LEGACY archives (rows whose files were moved into an archive home by
the old implementation) back to their original cwd. Remove it together with
``_restore_legacy_archive`` once no such rows remain.

When source and destination live on the *same* filesystem the restore is an
instant atomic ``rename``. When they live on *different* filesystems (e.g.
archive on the root partition, worktrees on a data drive), the copy is guarded
by a free-space/inode pre-check so a restore can never drive a disk to 0, and
a copy that fails partway is cleaned up rather than left as a partial tree.
"""
from __future__ import annotations

import errno
import logging
import os
import shutil
from pathlib import Path

log = logging.getLogger(__name__)

# Required free headroom (beyond the copy itself) before a cross-device copy
# is allowed to start, so restoring can never drive a disk to 0.
FREE_SPACE_MARGIN_BYTES = 1 * 1024 * 1024 * 1024  # 1 GiB

# Minimum available inodes on the destination filesystem before a cross-device
# copy is allowed. shutil.disk_usage() only checks bytes; inode exhaustion
# produces the same ENOSPC errno but silently slips past the byte guard.
FREE_INODE_MARGIN: int = 50_000


def mountpoint(path: Path) -> Path:
    """Mount point of the filesystem containing *path*.

    Walks to the nearest existing ancestor first (a restore target may not exist
    yet), then up to the filesystem boundary. Used to name the *actual* full disk
    in out-of-space errors — ``Path.anchor`` is always ``/`` and misleads on hosts
    whose session data lives on a separate mount (e.g. ``/mnt/ssk-ssd``).
    """
    p = path
    while not p.exists() and p != p.parent:
        p = p.parent
    p = Path(os.path.realpath(p))
    while not os.path.ismount(p) and p != p.parent:
        p = p.parent
    return p


def _tree_size(path: Path) -> int:
    """Apparent byte size of *path* (what a copy costs)."""
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass  # vanished/unreadable mid-walk — ignore, estimate is a guard
    return total


def restore_from_archive(
    src: Path,
    dest: Path,
    *,
    margin: int = FREE_SPACE_MARGIN_BYTES,
) -> str:
    """Move a legacy archive directory *src* back to its original cwd *dest*.

    Same filesystem: atomic ``os.rename`` (instant).

    Cross filesystem: copy guarded by a free-space pre-check, then remove
    *src*. A copy that fails partway is cleaned up so it never leaves a
    partial tree hogging the destination disk.

    Returns ``"renamed"``, ``"copied"``, or ``"skipped:nospace"``. Re-raises
    genuinely unexpected ``OSError``\\ s (anything other than the EXDEV that
    signals "different filesystem").
    """
    dest.parent.mkdir(parents=True, exist_ok=True)

    try:
        os.rename(src, dest)
        return "renamed"
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        # Different filesystem — fall through to a guarded copy.

    need = _tree_size(src)
    free = shutil.disk_usage(dest.parent).free
    if free < need + margin:
        return "skipped:nospace"

    try:
        if os.statvfs(dest.parent).f_favail < FREE_INODE_MARGIN:
            return "skipped:nospace"
    except OSError:
        pass  # can't probe — proceed and let copytree raise naturally

    try:
        shutil.copytree(src, dest,
                        symlinks=True, ignore_dangling_symlinks=True)
    except shutil.Error as exc:
        # Individual-file failures (e.g. symlink metadata on SSHFS, special files the
        # filesystem can't represent).  The core session content is intact — log each
        # failed path and continue so the restored worktree exists.  A hard OSError
        # (disk full, permissions) is still fatal.
        failures = exc.args[0] if exc.args else []
        log.warning(
            "archive_move: %d file(s) failed to copy but restore will proceed "
            "(src=%s dest=%s): %s",
            len(failures), src, dest, failures,
        )
    except OSError:
        shutil.rmtree(dest, ignore_errors=True)  # never leave a partial copy behind
        raise
    shutil.rmtree(src)
    return "copied"
