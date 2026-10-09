import pytest

from server.library import scan
from server.mcp_scope.resolve import effective_disabled
from server.mcp_scope.servers import configured_server_keys, server_key

ALL = ["a", "b", "c", "plugin:p:d"]


@pytest.mark.parametrize("defaults,rules,overrides,expected", [
    ({}, {}, {}, []),                                         # implicit: everything on
    ({"a": False}, {}, {}, ["a"]),                            # default off
    ({"a": False}, {"a": True}, {}, []),                      # repo rule beats default
    ({}, {"b": False}, {}, ["b"]),                            # repo rule turns a server off
    ({"a": False}, {"a": True}, {"a": False}, ["a"]),         # session beats repo
    ({}, {"b": False}, {"b": True}, []),                      # session beats repo (on)
    ({"plugin:p:d": False}, {}, {}, ["plugin:p:d"]),          # plugin keys are first-class
])
def test_precedence(defaults, rules, overrides, expected):
    assert effective_disabled(ALL, server_defaults=defaults, repo_rules=rules,
                              session_overrides=overrides) == expected


def test_unknown_keys_in_rules_are_ignored():
    assert effective_disabled(["a"], server_defaults={"gone": False}, repo_rules={"gone": True},
                              session_overrides={"gone": False}) == []


def test_output_is_sorted_and_deduplicated():
    assert effective_disabled(["b", "a", "a"], server_defaults={"a": False, "b": False},
                              repo_rules={}, session_overrides={}) == ["a", "b"]


def test_server_key_user_and_plugin():
    assert server_key({"name": "gh", "source": "user"}) == "gh"
    assert server_key({"name": "railway", "source": "railway@claude-plugins-official",
                       "pluginName": "railway"}) == "plugin:railway:railway"


def test_server_key_uses_manifest_name_not_registry_id():
    # Spike addendum (Task 3), docs/superpowers/spikes/2026-10-09-mcp-repo-scope-verification.md:
    # registry notion@claude-plugins-official has manifest name "Notion"; the CLI reports plugin:Notion:notion.
    entry = {"name": "notion", "source": "notion@claude-plugins-official", "pluginName": "Notion"}
    assert server_key(entry) == "plugin:Notion:notion"


def test_user_and_plugin_servers_with_same_short_name_get_distinct_keys():
    user = {"name": "notion", "source": "user"}
    plugin = {"name": "notion", "source": "notion@claude-plugins-official", "pluginName": "Notion"}
    assert server_key(user) == "notion"
    assert server_key(plugin) == "plugin:Notion:notion"
    assert server_key(user) != server_key(plugin)


def test_configured_server_keys_sorts_and_dedupes(monkeypatch):
    fake = {"servers": [
        {"name": "zeta", "source": "user"},
        {"name": "alpha", "source": "user"},
        {"name": "alpha", "source": "user"},
        {"name": "notion", "source": "notion@claude-plugins-official", "pluginName": "Notion"},
        {"name": "notion", "source": "notion@claude-plugins-official", "pluginName": "Notion"},
    ]}
    monkeypatch.setattr(scan, "list_mcp_servers", lambda status_rows=None: fake)
    assert configured_server_keys() == ["alpha", "plugin:Notion:notion", "zeta"]
