"""Library mutations — atomic writes into the shared base and store."""
import json
from pathlib import Path

import pytest

from ccswitch import paths
from server.library import mutate, scan


@pytest.fixture()
def shared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    base = tmp_path / "shared"
    base.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(base))
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path / "home"))
    (tmp_path / "home" / "store").mkdir(parents=True)
    return base


def test_set_plugin_enabled_roundtrip(shared: Path):
    (shared / "settings.json").write_text(json.dumps({
        "enabledPlugins": {"a@m": True},
        "permissions": {"allow": ["Bash"]},   # must survive untouched
    }))
    assert mutate.set_plugin_enabled("a@m", False) is True
    data = json.loads((shared / "settings.json").read_text())
    assert data["enabledPlugins"]["a@m"] is False
    assert data["permissions"] == {"allow": ["Bash"]}
    assert mutate.set_plugin_enabled("unknown@m", True) is False


def test_set_plugin_enabled_known_via_registry(shared: Path):
    (shared / "plugins").mkdir()
    (shared / "plugins" / "installed_plugins.json").write_text(json.dumps(
        {"version": 2, "plugins": {"b@m": [{"installPath": "/x", "version": "1"}]}}
    ))
    assert mutate.set_plugin_enabled("b@m", True) is True
    data = json.loads((shared / "settings.json").read_text())
    assert data["enabledPlugins"]["b@m"] is True


def test_skill_crud(shared: Path):
    mutate.write_skill("new-skill", "---\nname: new-skill\n---\nbody\n")
    created = scan.read_skill("new-skill")
    assert created is not None and created["content"].endswith("body\n")
    mutate.write_skill("new-skill", "updated\n")
    updated = scan.read_skill("new-skill")
    assert updated is not None and updated["content"] == "updated\n"
    assert mutate.delete_skill("new-skill") is True
    assert scan.read_skill("new-skill") is None
    assert mutate.delete_skill("new-skill") is False


def test_command_crud_nested(shared: Path):
    mutate.write_command("project/deep", "cmd\n")
    deep = scan.read_command("project/deep")
    assert deep is not None and deep["content"] == "cmd\n"
    assert mutate.delete_command("project/deep") is True
    assert mutate.delete_command("project/deep") is False


def test_traversal_rejected(shared: Path):
    with pytest.raises(ValueError):
        mutate.write_skill("../evil", "x")
    with pytest.raises(ValueError):
        mutate.write_command("../../etc/passwd", "x")


def test_mcp_crud(shared: Path):
    mutate.put_mcp_server("srv", {"type": "http", "url": "https://x/mcp"})
    data = json.loads(paths.shared_claude_json_path().read_text())
    assert data["mcpServers"]["srv"]["url"] == "https://x/mcp"
    mutate.put_mcp_server("srv", {"command": "bin", "args": ["-v"]})
    data = json.loads(paths.shared_claude_json_path().read_text())
    assert data["mcpServers"]["srv"] == {"command": "bin", "args": ["-v"]}
    assert mutate.delete_mcp_server("srv") is True
    assert mutate.delete_mcp_server("srv") is False


def test_mcp_preserves_other_shared_keys(shared: Path):
    from ccswitch.creds import write_json_secure
    write_json_secure(paths.shared_claude_json_path(), {"projects": {"/x": {}}})
    mutate.put_mcp_server("srv", {"command": "bin"})
    data = json.loads(paths.shared_claude_json_path().read_text())
    assert data["projects"] == {"/x": {}}


def test_validate_mcp_config():
    assert mutate.validate_mcp_config({"command": "bin"}) is None
    assert mutate.validate_mcp_config({"type": "http", "url": "https://x"}) is None
    assert mutate.validate_mcp_config({"type": "http"}) is not None      # no url
    assert mutate.validate_mcp_config({"type": "stdio"}) is not None     # no command
    assert mutate.validate_mcp_config({"command": ""}) is not None
    assert mutate.validate_mcp_config({"command": "b", "args": "notalist"}) is not None
    assert mutate.validate_mcp_config({"type": "sse", "url": "https://x", "headers": {"A": "b"}}) is None
