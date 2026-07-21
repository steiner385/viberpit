"""Cross-device-safe archiving of a session worktree.

Archiving moves a session's working directory into the archive dir. When the
two live on the *same* filesystem this is an instant atomic ``rename``. When
they live on *different* filesystems (e.g. worktrees on a data drive, archive
on the root partition), ``shutil.move`` silently falls back to a recursive
copy — and copying a worktree's ``node_modules`` across the boundary can fill
the destination disk, leaving a multi-gigabyte partial copy behind. That is
exactly what broke a deploy: the root partition filled mid-archive and the
next daemon boot could not write per-session config, so no sessions restored.

``move_into_archive`` makes the cross-device path safe: prune regenerable
dependency dirs, refuse to start if the destination lacks headroom, and clean
up any partial copy on failure.
"""
from __future__ import annotations

import errno
import os
import shutil
from pathlib import Path

# Regenerable dependency dirs — never worth archiving; an unarchived session
# just reinstalls them. Pruned on BOTH move paths: across a filesystem boundary
# they'd bloat the copy, and even a free same-device rename permanently parks
# their INODES in the archive (200 auto-archived KinDash sessions exhausted the
# data disk's inode table, killing every new session's `git worktree add`).
PRUNE_DIRS: tuple[str, ...] = ("node_modules", ".venv")

# Required free headroom (beyond the copy itself) before a cross-device copy
# is allowed to start, so archiving can never drive a disk to 0.
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


def _prune_regenerable(src: Path, prune: tuple[str, ...]) -> None:
    """Delete *prune*-named dirs anywhere under *src* (best-effort)."""
    for root, dirs, _files in os.walk(src):
        for name in [d for d in dirs if d in prune]:
            shutil.rmtree(os.path.join(root, name), ignore_errors=True)
            dirs.remove(name)


def _tree_size(path: Path, prune: tuple[str, ...]) -> int:
    """Apparent byte size of *path*, skipping *prune* dirs (what a copy costs)."""
    total = 0
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in prune]
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass  # vanished/unreadable mid-walk — ignore, estimate is a guard
    return total


def move_into_archive(
    src: Path,
    dest: Path,
    *,
    prune: tuple[str, ...] = PRUNE_DIRS,
    margin: int = FREE_SPACE_MARGIN_BYTES,
) -> str:
    """Move directory *src* to *dest*, pruning regenerable *prune* dirs first.

    The prune happens on BOTH paths — a same-device rename is free in bytes
    but permanently parks the pruned dirs' inodes in the archive otherwise,
    and the cross-device copy would pay for them in bytes. Either way the
    archive lands dependency-free; unarchiving reinstalls.

    Same filesystem: atomic ``os.rename`` (instant).

    Cross filesystem: copy guarded by a free-space pre-check, then remove
    *src*. A copy that fails partway is cleaned up so it never leaves a
    partial tree hogging the destination disk.

    Returns ``"renamed"``, ``"copied"``, or ``"skipped:nospace"``. Re-raises
    genuinely unexpected ``OSError``\\ s (anything other than the EXDEV that
    signals "different filesystem").
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    _prune_regenerable(src, prune)

    try:
        os.rename(src, dest)
        return "renamed"
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        # Different filesystem — fall through to a guarded, pruned copy.

    need = _tree_size(src, prune)
    free = shutil.disk_usage(dest.parent).free
    if free < need + margin:
        return "skipped:nospace"

    try:
        if os.statvfs(dest.parent).f_favail < FREE_INODE_MARGIN:
            return "skipped:nospace"
    except OSError:
        pass  # can't probe — proceed and let copytree raise naturally

    try:
        shutil.copytree(src, dest, ignore=shutil.ignore_patterns(*prune), symlinks=True)
    except OSError:
        shutil.rmtree(dest, ignore_errors=True)  # never leave a partial copy behind
        raise
    shutil.rmtree(src)
    return "copied"
