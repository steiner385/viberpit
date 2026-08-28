"""Tests for restore_from_archive — cross-device-safe restore of LEGACY archives.

@deprecated together with server/sessions/archive_move.py — archiving no longer
moves worktrees, so this module only restores rows archived by the old
implementation. Regression coverage for the disk-full incident survives here in
the restore direction: a cross-device restore is guarded by a free-space
pre-check and never leaves a partial copy behind.
"""
from __future__ import annotations

import errno
import os
import shutil
from pathlib import Path

import pytest

from server.sessions.archive_move import mountpoint, restore_from_archive


def test_mountpoint_resolves_to_filesystem_boundary(tmp_path):
    """mountpoint() returns a real mount point that is an ancestor of the target,
    even when the target path doesn't exist yet (a restore target may not)."""
    target = tmp_path / "a" / "b" / "c"  # deliberately does not exist
    mp = mountpoint(target)
    assert os.path.ismount(mp)  # it IS an actual filesystem boundary
    # …and an ancestor of the target's nearest-existing realpath
    assert os.path.commonpath([str(mp), os.path.realpath(tmp_path)]) == str(mp)


def test_mountpoint_of_nonexistent_absolute_path_is_root():
    """A path with no existing ancestor but root walks up to '/', not a crash."""
    assert str(mountpoint(Path("/nonexistent/deep/path"))) == "/"


def _make_archived_tree(base: Path) -> Path:
    """A legacy archived worktree (already dependency-pruned at archive time)."""
    src = base / "archived" / "sess"
    (src / "src").mkdir(parents=True)
    (src / "src" / "app.py").write_text("print('hi')\n")
    (src / ".git").mkdir()
    (src / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    return src


def _raise_exdev(*_a, **_k):
    raise OSError(errno.EXDEV, "Invalid cross-device link")


class _Usage:
    def __init__(self, free: int) -> None:
        self.free = free


def test_same_device_uses_rename(tmp_path):
    src = _make_archived_tree(tmp_path)
    dest = tmp_path / "worktrees" / "sess1"

    assert restore_from_archive(src, dest) == "renamed"

    assert not src.exists()
    assert (dest / "src" / "app.py").exists()      # real work preserved
    assert (dest / ".git" / "HEAD").exists()       # history preserved


def test_cross_device_copies_whole_tree(tmp_path, monkeypatch):
    src = _make_archived_tree(tmp_path)
    dest = tmp_path / "worktrees" / "sess2"
    monkeypatch.setattr(os, "rename", _raise_exdev)
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: _Usage(free=10**12))

    assert restore_from_archive(src, dest) == "copied"

    assert not src.exists()
    assert (dest / "src" / "app.py").exists()      # real work preserved
    assert (dest / ".git" / "HEAD").exists()       # history preserved


def test_cross_device_skips_when_insufficient_space(tmp_path, monkeypatch):
    src = _make_archived_tree(tmp_path)
    dest = tmp_path / "worktrees" / "sess3"
    monkeypatch.setattr(os, "rename", _raise_exdev)
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: _Usage(free=1))  # disk basically full

    assert restore_from_archive(src, dest) == "skipped:nospace"

    assert src.exists()        # left in place, never destroyed
    assert not dest.exists()   # and no partial copy created


def test_cross_device_skips_when_inodes_low(tmp_path, monkeypatch):
    """The inode guard: bytes look fine but the destination is out of inodes."""
    src = _make_archived_tree(tmp_path)
    dest = tmp_path / "worktrees" / "sess_inode"
    monkeypatch.setattr(os, "rename", _raise_exdev)
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: _Usage(free=10**12))

    class _SV:
        f_favail = 1  # below FREE_INODE_MARGIN

    monkeypatch.setattr(os, "statvfs", lambda _p: _SV())

    assert restore_from_archive(src, dest, margin=0) == "skipped:nospace"
    assert not dest.exists()   # no partial copy left behind


def test_cross_device_cleans_partial_copy_on_failure(tmp_path, monkeypatch):
    """The guard passes but the copy dies mid-way (ENOSPC). The half-written
    destination must not be left hogging disk."""
    src = _make_archived_tree(tmp_path)
    dest = tmp_path / "worktrees" / "sess4"
    monkeypatch.setattr(os, "rename", _raise_exdev)
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: _Usage(free=10**12))

    def boom(*_a, **_k):
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "partial").write_text("half a worktree")
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(shutil, "copytree", boom)

    with pytest.raises(OSError):
        restore_from_archive(src, dest)

    assert not dest.exists()   # partial cleaned up
    assert src.exists()        # source preserved for a retry


def test_cross_device_dangling_symlink_is_preserved(tmp_path, monkeypatch):
    """An archive with a dangling symlink (e.g. certbot cert) restores successfully.

    Regression: shutil.copytree raised shutil.Error for dangling symlinks on SSHFS
    mounts, which triggered cleanup of the partial directory.
    """
    src = _make_archived_tree(tmp_path)
    # Dangling symlink — target does not exist (certbot / .git/config style).
    (src / "cert.pem").symlink_to("/etc/letsencrypt/live/example.com/cert.pem")
    dest = tmp_path / "worktrees" / "sess6"
    monkeypatch.setattr(os, "rename", _raise_exdev)
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: _Usage(free=10**12))

    result = restore_from_archive(src, dest)

    assert result == "copied"
    assert not src.exists()             # source removed
    assert dest.is_dir()                # restored directory exists
    assert (dest / "src" / "app.py").exists()
    # Symlink recreated at destination (dangling is fine — the target path is preserved).
    assert (dest / "cert.pem").is_symlink()


def test_cross_device_individual_file_failures_do_not_abort_restore(tmp_path, monkeypatch):
    """shutil.Error (individual-file failures, e.g. SSHFS xattr) does not abort the restore.

    Regression: an except OSError: clause caught shutil.Error (a subclass) and
    cleaned up the partial dest directory, losing the restorable content.
    """
    src = _make_archived_tree(tmp_path)
    dest = tmp_path / "worktrees" / "sess7"
    monkeypatch.setattr(os, "rename", _raise_exdev)
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: _Usage(free=10**12))

    # Simulate SSHFS-style per-file metadata failures: copytree creates the dest
    # directory with most files but raises shutil.Error for one entry.
    def partial_copytree(src_arg, dest_arg, **kwargs):
        dest_arg.mkdir(parents=True, exist_ok=True)
        (dest_arg / "src").mkdir(parents=True, exist_ok=True)
        (dest_arg / "src" / "app.py").write_text("print('hi')\n")
        raise shutil.Error([(str(src_arg / "cert.pem"), str(dest_arg / "cert.pem"),
                             "Operation not supported")])

    monkeypatch.setattr(shutil, "copytree", partial_copytree)

    result = restore_from_archive(src, dest)

    assert result == "copied"
    assert dest.is_dir()                # restored directory exists despite the error
    assert not src.exists()             # source still removed after partial-success copy
