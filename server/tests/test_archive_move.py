"""Tests for move_into_archive — the cross-device-safe worktree archiver.

Regression coverage for the disk-full incident: archiving a session whose
worktree lives on a different filesystem than the archive dir used to do a
plain ``shutil.move``, which fell back to a recursive copy of ``node_modules``
and filled the destination disk (leaving a multi-GB partial copy behind).
"""
from __future__ import annotations

import errno
import os
import shutil
from pathlib import Path

import pytest

from server.sessions.archive_move import move_into_archive


def _make_tree(base: Path) -> Path:
    """A worktree with real work plus a fat, regenerable node_modules tree."""
    src = base / "wt"
    (src / "src").mkdir(parents=True)
    (src / "src" / "app.py").write_text("print('hi')\n")
    (src / ".git").mkdir()
    (src / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    nm = src / "node_modules" / ".pnpm" / "pkg"
    nm.mkdir(parents=True)
    (nm / "big.js").write_text("x" * 10_000)
    return src


def _raise_exdev(*_a, **_k):
    raise OSError(errno.EXDEV, "Invalid cross-device link")


class _Usage:
    def __init__(self, free: int) -> None:
        self.free = free


def test_same_device_uses_rename_and_keeps_everything(tmp_path):
    src = _make_tree(tmp_path)
    dest = tmp_path / "archive" / "sess1"

    assert move_into_archive(src, dest) == "renamed"

    assert not src.exists()
    assert (dest / "src" / "app.py").exists()
    # A same-device rename is free, so it keeps node_modules — nothing to gain by pruning.
    assert (dest / "node_modules").exists()


def test_cross_device_prunes_node_modules(tmp_path, monkeypatch):
    src = _make_tree(tmp_path)
    dest = tmp_path / "archive" / "sess2"
    monkeypatch.setattr(os, "rename", _raise_exdev)
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: _Usage(free=10**12))

    assert move_into_archive(src, dest) == "copied"

    assert not src.exists()
    assert (dest / "src" / "app.py").exists()      # real work preserved
    assert (dest / ".git" / "HEAD").exists()       # history preserved
    assert not (dest / "node_modules").exists()    # regenerable deps pruned


def test_cross_device_skips_when_insufficient_space(tmp_path, monkeypatch):
    src = _make_tree(tmp_path)
    dest = tmp_path / "archive" / "sess3"
    monkeypatch.setattr(os, "rename", _raise_exdev)
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: _Usage(free=1))  # disk basically full

    assert move_into_archive(src, dest) == "skipped:nospace"

    assert src.exists()        # left in place, never destroyed
    assert not dest.exists()   # and no partial copy created


def test_cross_device_cleans_partial_copy_on_failure(tmp_path, monkeypatch):
    """The exact incident shape: the guard passes but the copy dies mid-way
    (ENOSPC). The half-written destination must not be left hogging disk."""
    src = _make_tree(tmp_path)
    dest = tmp_path / "archive" / "sess4"
    monkeypatch.setattr(os, "rename", _raise_exdev)
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: _Usage(free=10**12))

    def boom(*_a, **_k):
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "partial").write_text("half a node_modules")
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(shutil, "copytree", boom)

    with pytest.raises(OSError):
        move_into_archive(src, dest)

    assert not dest.exists()   # partial cleaned up
    assert src.exists()        # source preserved for a retry
