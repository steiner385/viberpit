"""Library scan functions over a fabricated shared base + store."""
import json
from pathlib import Path

import pytest

from ccswitch import paths
from server.library import scan


@pytest.fixture()
def shared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated shared base + ccswitch home. NEVER touch the real ~/.claude."""
    base = tmp_path / "shared"
    base.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(base))
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path / "home"))
    (tmp_path / "home" / "store").mkdir(parents=True)
    return base


def _seed_plugin(base: Path, name: str = "alpha", enabled: bool = True) -> Path:
    """Install one plugin with a command, a skill, and an MCP server."""
    pid = f"{name}@test-market"
    install = base / "plugins" / "cache" / "test-market" / name / "1.0.0"
    (install / "commands").mkdir(parents=True)
    (install / "commands" / "run.md").write_text("---\ndescription: Run it\n---\nbody\n")
    (install / "skills" / "helper").mkdir(parents=True)
    (install / "skills" / "helper" / "SKILL.md").write_text(
        "---\nname: helper\ndescription: Helps\n---\nbody\n"
    )
    (install / ".mcp.json").write_text(json.dumps(
        {"mcpServers": {"alpha-srv": {"command": "alpha-bin", "args": []}}}
    ))
    (base / "plugins").mkdir(exist_ok=True)
    (base / "plugins" / "installed_plugins.json").write_text(json.dumps({
        "version": 2,
        "plugins": {pid: [{
            "scope": "user", "installPath": str(install), "version": "1.0.0",
            "installedAt": "2026-01-01T00:00:00Z", "lastUpdated": "2026-06-01T00:00:00Z",
        }]},
    }))
    (base / "settings.json").write_text(json.dumps({"enabledPlugins": {pid: enabled}}))
    (base / "plugins" / "known_marketplaces.json").write_text(json.dumps(
        {"test-market": {"source": "github:test/market", "lastUpdated": "2026-06-01"}}
    ))
    return install


def test_empty_world_is_empty_not_error(shared: Path):
    assert scan.list_plugins() == {"plugins": [], "marketplaces": []}
    assert scan.list_skills() == {"skills": []}
    assert scan.list_commands() == {"commands": []}
    assert scan.list_mcp_servers() == {"servers": []}


def test_list_plugins_merges_registry_and_enabled(shared: Path):
    _seed_plugin(shared, "alpha", enabled=True)
    out = scan.list_plugins()
    assert len(out["plugins"]) == 1
    p = out["plugins"][0]
    assert p["id"] == "alpha@test-market"
    assert p["name"] == "alpha"
    assert p["marketplace"] == "test-market"
    assert p["enabled"] is True
    assert p["version"] == "1.0.0"
    assert out["marketplaces"] == [
        {"name": "test-market", "source": "github:test/market", "lastUpdated": "2026-06-01"}
    ]


def test_plugin_detail_scans_components(shared: Path):
    _seed_plugin(shared, "alpha")
    d = scan.plugin_detail("alpha@test-market")
    assert d is not None
    assert d["commands"] == [{"path": "run", "description": "Run it"}]
    assert d["skills"] == [{"name": "helper", "description": "Helps"}]
    assert d["mcpServers"] == [{"name": "alpha-srv", "transport": "stdio"}]
    assert scan.plugin_detail("nope@nowhere") is None


def test_list_skills_user_and_plugin(shared: Path):
    _seed_plugin(shared, "alpha")
    (shared / "skills" / "my-skill").mkdir(parents=True)
    (shared / "skills" / "my-skill" / "SKILL.md").write_text(
        "---\nname: my-skill\ndescription: Mine\n---\nbody\n"
    )
    out = scan.list_skills()["skills"]
    assert [(s["name"], s["source"], s["readonly"]) for s in out] == [
        ("my-skill", "user", False),
        ("helper", "alpha@test-market", True),
    ]
    mine = scan.read_skill("my-skill")
    assert mine is not None and mine["content"].endswith("body\n")
    helper = scan.read_skill("helper", plugin="alpha@test-market")
    assert helper is not None and helper["readonly"] is True
    assert scan.read_skill("absent") is None


def test_malformed_frontmatter_flags_not_raises(shared: Path):
    (shared / "skills" / "broken").mkdir(parents=True)
    (shared / "skills" / "broken" / "SKILL.md").write_text("---\n: {invalid\n---\nx\n")
    out = scan.list_skills()["skills"]
    assert out[0]["name"] == "broken"
    assert out[0]["parseError"] is True


def test_list_commands_nested(shared: Path):
    (shared / "commands" / "project").mkdir(parents=True)
    (shared / "commands" / "top.md").write_text("---\ndescription: Top\n---\nx\n")
    (shared / "commands" / "project" / "nested.md").write_text("x\n")
    out = scan.list_commands()["commands"]
    assert [(c["path"], c["source"]) for c in out] == [
        ("project/nested", "user"), ("top", "user"),
    ]
    nested = scan.read_command("project/nested")
    assert nested is not None and nested["content"] == "x\n"


def test_list_mcp_servers_user_plugin_and_auth(shared: Path, tmp_path: Path):
    _seed_plugin(shared, "alpha")
    from ccswitch.creds import write_json_secure
    write_json_secure(paths.shared_claude_json_path(), {"mcpServers": {
        "mine": {"type": "http", "url": "https://x.example/mcp"},
    }})
    acct = paths.cfg_root() / "acct1"
    acct.mkdir(parents=True)
    (acct / "mcp-needs-auth-cache.json").write_text(json.dumps({
        "plugin:alpha:alpha-srv": {"timestamp": 1}, "mine": {"timestamp": 2},
    }))
    out = scan.list_mcp_servers()["servers"]
    by_name = {s["name"]: s for s in out}
    assert by_name["mine"]["source"] == "user"
    assert by_name["mine"]["transport"] == "http"
    assert by_name["mine"]["needsAuth"] == ["acct1"]
    assert by_name["alpha-srv"]["readonly"] is True
    assert by_name["alpha-srv"]["needsAuth"] == ["acct1"]


def test_read_traversal_rejected(shared: Path):
    (shared / "skills").mkdir()
    (shared / "commands").mkdir()
    assert scan.read_skill("../evil") is None
    assert scan.read_command("../evil") is None
    assert scan.read_command("a/../../evil") is None


def test_list_plugins_token_cost_from_catalog(shared: Path):
    """tokenCost is derived from catalog tokens, taking MAX across models."""
    _seed_plugin(shared, "alpha", enabled=True)
    pid = "alpha@test-market"
    catalog_data = {
        "catalog": {
            "plugins": {
                pid: {
                    "tokens": {
                        "claude-3-5-sonnet": {"always_on": 200, "on_invoke": 100},
                        "claude-3-haiku": {"always_on": 150, "on_invoke": 80},
                    }
                }
            }
        }
    }
    (shared / "plugins" / "plugin-catalog-cache.json").write_text(json.dumps(catalog_data))
    out = scan.list_plugins()
    p = next(pl for pl in out["plugins"] if pl["id"] == pid)
    assert p["tokenCost"] == {"alwaysOn": 200, "onInvoke": 100}


def test_list_plugins_no_tokens_is_none(shared: Path):
    """Plugin with no tokens field in catalog → tokenCost is None."""
    _seed_plugin(shared, "alpha", enabled=True)
    pid = "alpha@test-market"
    catalog_data = {
        "catalog": {
            "plugins": {
                pid: {
                    "marketplace_entry": {"description": "A plugin"},
                }
            }
        }
    }
    (shared / "plugins" / "plugin-catalog-cache.json").write_text(json.dumps(catalog_data))
    out = scan.list_plugins()
    p = next(pl for pl in out["plugins"] if pl["id"] == pid)
    assert p["tokenCost"] is None
