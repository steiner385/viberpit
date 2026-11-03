"""viberpit.fs_beneath: fd-relative, symlink-refusing opens."""
from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from viberpit.fs_beneath import SymlinkInPath, open_beneath, open_dir_beneath


@pytest.fixture
def root(tmp_path: Path) -> Iterator[tuple[Path, int]]:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "f").write_text("hi\n")
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    yield tmp_path, fd
    os.close(fd)


@pytest.mark.parametrize("rel", ["a/f", b"a/f"])
def test_opens_a_nested_file(root: tuple[Path, int], rel: str | bytes) -> None:
    fd = open_beneath(root[1], rel)
    with os.fdopen(fd, "rb") as fh:
        assert fh.read() == b"hi\n"


@pytest.mark.parametrize("rel", ["/etc/passwd", b"/etc/passwd", "a//f", "a/f/", "", b""])
def test_rejects_absolute_and_empty_component_paths(root: tuple[Path, int],
                                                    rel: str | bytes) -> None:
    with pytest.raises(ValueError):
        open_beneath(root[1], rel)


def test_refuses_a_symlinked_parent_and_leaf(root: tuple[Path, int], tmp_path: Path) -> None:
    os.symlink(tmp_path / "a", tmp_path / "link")
    os.symlink(tmp_path / "a" / "f", tmp_path / "leaf")
    with pytest.raises(SymlinkInPath):
        open_beneath(root[1], "link/f")
    with pytest.raises(SymlinkInPath):
        open_beneath(root[1], "leaf")


def test_create_refuses_before_making_anything_through_a_symlink(
        root: tuple[Path, int], tmp_path: Path) -> None:
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.mkdir()
    os.symlink(outside, tmp_path / "out")
    with pytest.raises(SymlinkInPath):
        open_dir_beneath(root[1], "out/x/y", create=True)
    assert list(outside.iterdir()) == []
    fd = open_dir_beneath(root[1], [b"new", b"dir"], create=True)
    os.close(fd)
    assert (tmp_path / "new" / "dir").is_dir()
