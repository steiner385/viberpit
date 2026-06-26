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

# Regenerable dependency dirs — never worth copying across a filesystem
# boundary; an unarchived session just reinstalls them.
PRUNE_DIRS: tuple[str, ...] = ("node_modules",)

# Required free headroom (beyond the copy itself) before a cross-device copy
# is allowed to start, so archiving can never drive a disk to 0.
FREE_SPACE_MARGIN_BYTES = 1 * 1024 * 1024 * 1024  # 1 GiB


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
    """Move directory *src* to *dest*.

    Same filesystem: atomic ``os.rename`` (instant, keeps everything including
    ``node_modules`` — there is nothing to gain by pruning a free rename).

    Cross filesystem: copy excluding regenerable *prune* dirs, guarded by a
    free-space pre-check, then remove *src*. A copy that fails partway is
    cleaned up so it never leaves a partial tree hogging the destination disk.

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
        # Different filesystem — fall through to a guarded, pruned copy.

    need = _tree_size(src, prune)
    free = shutil.disk_usage(dest.parent).free
    if free < need + margin:
        return "skipped:nospace"

    try:
        shutil.copytree(src, dest, ignore=shutil.ignore_patterns(*prune), symlinks=True)
    except OSError:
        shutil.rmtree(dest, ignore_errors=True)  # never leave a partial copy behind
        raise
    shutil.rmtree(src)
    return "copied"
