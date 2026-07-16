"""Hooks library — read/validate/write settings.json hooks block + dry-run."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ccswitch import paths
from server.library import hooks


@pytest.fixture()
def shared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    base = tmp_path / "shared"
    base.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(base))
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path / "home"))
    (tmp_path / "home" / "store").mkdir(parents=True)
    # Sanity: shared_base() must resolve to our tmp dir, not ~/.claude.
    assert paths.shared_base() == base
    return base


def _settings(shared: Path) -> dict:
    return json.loads((shared / "settings.json").read_text())


def test_set_then_list_roundtrip(shared: Path):
    rows: list[dict] = [
        {"event": "PreToolUse", "matcher": "Bash", "command": "echo hi", "enabled": True},
        {"event": "SessionStart", "matcher": None, "command": "echo start", "enabled": True},
    ]
    hooks.set_hooks(rows)
    got = hooks.list_hooks()["events"]
    assert got == rows

    # Nested settings shape is the Claude hooks format.
    data = _settings(shared)
    assert data["hooks"]["PreToolUse"] == [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi"}]}
    ]
    assert data["hooks"]["SessionStart"] == [
        {"hooks": [{"type": "command", "command": "echo start"}]}
    ]


def test_multiple_commands_same_matcher_group(shared: Path):
    rows = [
        {"event": "PreToolUse", "matcher": "Bash", "command": "a", "enabled": True},
        {"event": "PreToolUse", "matcher": "Bash", "command": "b", "enabled": True},
    ]
    hooks.set_hooks(rows)
    data = _settings(shared)
    groups = data["hooks"]["PreToolUse"]
    assert len(groups) == 1
    assert groups[0]["matcher"] == "Bash"
    assert [h["command"] for h in groups[0]["hooks"]] == ["a", "b"]
    assert hooks.list_hooks()["events"] == rows


def test_disabled_hooks_park_under_disabledHooks(shared: Path):
    rows = [
        {"event": "PostToolUse", "matcher": "Bash", "command": "on", "enabled": True},
        {"event": "PostToolUse", "matcher": "Bash", "command": "off", "enabled": False},
    ]
    hooks.set_hooks(rows)
    data = _settings(shared)
    assert "PostToolUse" in data["hooks"]
    assert "PostToolUse" in data["disabledHooks"]
    assert data["disabledHooks"]["PostToolUse"][0]["hooks"][0]["command"] == "off"

    got = hooks.list_hooks()["events"]
    # Enabled listed before disabled.
    assert got[0]["command"] == "on" and got[0]["enabled"] is True
    assert got[1]["command"] == "off" and got[1]["enabled"] is False


def test_list_hooks_missing_file_is_empty(shared: Path):
    assert hooks.list_hooks() == {"events": []}


def test_list_hooks_corrupt_file_is_empty(shared: Path):
    (shared / "settings.json").write_text("{ not json")
    assert hooks.list_hooks() == {"events": []}


def test_validate_rejects_bad_event():
    with pytest.raises(hooks.HooksValidationError):
        hooks.validate_hooks(
            [{"event": "Nope", "matcher": None, "command": "x", "enabled": True}]
        )


def test_validate_rejects_blank_command():
    with pytest.raises(hooks.HooksValidationError):
        hooks.validate_hooks(
            [{"event": "Stop", "matcher": None, "command": "   ", "enabled": True}]
        )
    with pytest.raises(hooks.HooksValidationError):
        hooks.validate_hooks(
            [{"event": "Stop", "matcher": None, "command": 5, "enabled": True}]
        )


def test_validate_rejects_unbalanced_quotes():
    with pytest.raises(hooks.HooksValidationError):
        hooks.validate_hooks(
            [{"event": "Stop", "matcher": None, "command": "echo 'unterminated", "enabled": True}]
        )


def test_validate_rejects_bad_matcher_and_enabled():
    with pytest.raises(hooks.HooksValidationError):
        hooks.validate_hooks(
            [{"event": "Stop", "matcher": 1, "command": "x", "enabled": True}]
        )
    with pytest.raises(hooks.HooksValidationError):
        hooks.validate_hooks(
            [{"event": "Stop", "matcher": None, "command": "x", "enabled": "yes"}]
        )


def test_bad_set_leaves_file_byte_identical(shared: Path):
    hooks.set_hooks(
        [{"event": "PreToolUse", "matcher": "Bash", "command": "echo hi", "enabled": True}]
    )
    before = (shared / "settings.json").read_bytes()

    with pytest.raises(hooks.HooksValidationError):
        hooks.set_hooks(
            [{"event": "BadEvent", "matcher": None, "command": "x", "enabled": True}]
        )
    after = (shared / "settings.json").read_bytes()
    assert before == after


def test_set_preserves_unrelated_keys(shared: Path):
    (shared / "settings.json").write_text(
        json.dumps({"enabledPlugins": {"a@m": True}, "permissions": {"allow": ["Bash"]}})
    )
    hooks.set_hooks(
        [{"event": "Stop", "matcher": None, "command": "echo done", "enabled": True}]
    )
    data = _settings(shared)
    assert data["enabledPlugins"] == {"a@m": True}
    assert data["permissions"] == {"allow": ["Bash"]}
    assert data["hooks"]["Stop"][0]["hooks"][0]["command"] == "echo done"


def test_set_empty_clears_blocks(shared: Path):
    hooks.set_hooks(
        [{"event": "Stop", "matcher": None, "command": "x", "enabled": True}]
    )
    hooks.set_hooks([])
    data = _settings(shared)
    assert "hooks" not in data
    assert "disabledHooks" not in data


def test_dry_run_cat_echoes_event_json():
    result = hooks.dry_run("cat")
    assert result["exitCode"] == 0
    assert result["timedOut"] is False
    payload = json.loads(result["stdout"])
    assert payload["hook_event_name"] == "PostToolUse"
    assert result["command"] == "cat"


def test_dry_run_custom_event():
    result = hooks.dry_run("cat", {"hook_event_name": "Stop", "custom": 1})
    assert result["exitCode"] == 0
    assert json.loads(result["stdout"])["custom"] == 1


def test_dry_run_exit_code_propagates():
    result = hooks.dry_run("exit 3")
    assert result["exitCode"] == 3
    assert result["timedOut"] is False


def test_dry_run_captures_stderr_never_raises():
    result = hooks.dry_run("echo boom >&2; exit 1")
    assert result["exitCode"] == 1
    assert "boom" in result["stderr"]
