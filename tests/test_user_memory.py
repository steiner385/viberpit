"""#3246: moving the shared user CLAUDE.md into the coherently-mounted account store."""
from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from viberpit import cli, user_memory


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    dot = tmp_path / "dot-claude"
    dot.mkdir()
    store = tmp_path / "store"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(dot))
    monkeypatch.setenv("VIBERPIT_HOME", str(store))
    return SimpleNamespace(home=dot / "CLAUDE.md", store=store / "shared" / "CLAUDE.md")


def test_paths_name_the_dotfile_and_the_store(world) -> None:
    assert user_memory.home_path() == world.home
    assert user_memory.store_path() == world.store
    assert not user_memory.is_moved()
    assert user_memory.edit_path() == world.home


def test_move_copies_the_bytes_and_mode_then_links_back(world) -> None:
    world.home.write_bytes(b"# rules\n")
    world.home.chmod(0o640)
    assert user_memory.move_to_store() == user_memory.MOVED
    assert world.home.is_symlink() and os.readlink(world.home) == str(world.store)
    assert world.store.read_bytes() == b"# rules\n"
    assert not world.store.is_symlink()
    assert stat.S_IMODE(world.store.stat().st_mode) == 0o640
    assert world.home.read_bytes() == b"# rules\n"  # the CLI's name still reads it
    assert user_memory.is_moved() and user_memory.edit_path() == world.store
    # no temp link or temp file left behind
    assert sorted(p.name for p in world.home.parent.iterdir()) == ["CLAUDE.md"]
    assert sorted(p.name for p in world.store.parent.iterdir()) == ["CLAUDE.md"]


def test_move_is_idempotent(world) -> None:
    world.home.write_text("x\n")
    user_memory.move_to_store()
    assert user_memory.move_to_store() == user_memory.ALREADY
    assert world.store.read_text() == "x\n"


def test_move_with_no_file_only_links(world) -> None:
    assert user_memory.move_to_store() == user_memory.LINKED
    assert world.home.is_symlink() and not world.store.exists()
    assert user_memory.edit_path() == world.store


def test_move_refuses_a_link_elsewhere(world, tmp_path: Path) -> None:
    other = tmp_path / "dotfiles-CLAUDE.md"
    other.write_text("mine\n")
    world.home.symlink_to(other)
    with pytest.raises(user_memory.MoveRefused):
        user_memory.move_to_store()
    assert os.readlink(world.home) == str(other) and not world.store.exists()


def test_move_refuses_a_different_store_file(world) -> None:
    world.home.write_text("home\n")
    world.store.parent.mkdir(parents=True)
    world.store.write_text("store\n")
    with pytest.raises(user_memory.MoveRefused):
        user_memory.move_to_store()
    assert not world.home.is_symlink() and world.home.read_text() == "home\n"
    assert world.store.read_text() == "store\n"


def test_move_over_an_identical_store_file_links(world) -> None:
    world.home.write_text("same\n")
    world.store.parent.mkdir(parents=True)
    world.store.write_text("same\n")
    assert user_memory.move_to_store() == user_memory.MOVED
    assert user_memory.is_moved()


def test_an_edit_during_the_copy_aborts_and_leaves_no_store_copy(
        world, monkeypatch: pytest.MonkeyPatch) -> None:
    world.home.write_text("v0\n")
    real = user_memory.replace_file_beneath

    def racing(*a, **k):
        real(*a, **k)
        world.home.write_text("v1 — saved mid-move\n")

    monkeypatch.setattr(user_memory, "replace_file_beneath", racing)
    with pytest.raises(user_memory.MoveRefused):
        user_memory.move_to_store()
    assert not world.home.is_symlink()
    assert world.home.read_text() == "v1 — saved mid-move\n"
    assert not world.store.exists()


def test_cli_status_and_move(world, capsys: pytest.CaptureFixture[str]) -> None:
    world.home.write_text("x\n")
    assert cli.main(["memory", "status"]) == 0
    assert "not moved" in capsys.readouterr().out
    assert cli.main(["memory", "move"]) == 0
    assert "moved" in capsys.readouterr().out
    assert cli.main(["memory", "status"]) == 0
    assert "coherent on every host" in capsys.readouterr().out


def test_cli_move_refusal_exits_1(world, tmp_path: Path,
                                  capsys: pytest.CaptureFixture[str]) -> None:
    world.home.symlink_to(tmp_path / "elsewhere")
    assert cli.main(["memory", "move"]) == 1
    assert "refused" in capsys.readouterr().err
