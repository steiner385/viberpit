"""Identifier validation for library entities — path-traversal defense."""
from pathlib import Path

from server.library.names import (
    resolve_under,
    valid_command_path,
    valid_mcp_name,
    valid_plugin_id,
    valid_skill_name,
)


def test_skill_names():
    assert valid_skill_name("my-skill")
    assert valid_skill_name("a")
    assert not valid_skill_name("My-Skill")     # uppercase
    assert not valid_skill_name("-lead")        # leading dash
    assert not valid_skill_name("has space")
    assert not valid_skill_name("")
    assert not valid_skill_name("a" * 65)
    assert not valid_skill_name("../etc")


def test_command_paths():
    assert valid_command_path("gh-fix")
    assert valid_command_path("project/gh-fix")
    assert valid_command_path("a/b/c/d")
    assert not valid_command_path("a/b/c/d/e")   # >4 segments
    assert not valid_command_path("gh-fix.md")   # extension is implicit
    assert not valid_command_path("../evil")
    assert not valid_command_path("a/../b")
    assert not valid_command_path("/abs")
    assert not valid_command_path("trail/")
    assert not valid_command_path("")


def test_mcp_and_plugin_ids():
    assert valid_mcp_name("github-issue-priority")
    assert valid_mcp_name("My.Server_1")
    assert not valid_mcp_name("has space")
    assert not valid_mcp_name("")
    assert valid_plugin_id("code-review@claude-plugins-official")
    assert not valid_plugin_id("code-review")            # no marketplace
    assert not valid_plugin_id("a@b@c")
    assert not valid_plugin_id("UPPER@market")


def test_resolve_under(tmp_path: Path):
    base = tmp_path / "base"
    base.mkdir()
    assert resolve_under(base, "x", "y.md") == (base / "x" / "y.md").resolve()
    assert resolve_under(base, "..", "escape") is None
    assert resolve_under(base, "x/../../escape") is None
    # symlink escape: base/link -> tmp_path
    (base / "link").symlink_to(tmp_path)
    assert resolve_under(base, "link", "f") is None
