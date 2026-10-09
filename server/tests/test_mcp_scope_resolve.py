import pytest

from server.mcp_scope.resolve import effective_disabled
from server.mcp_scope.servers import server_key

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
    # Spike 2026-10-09 (docs/superpowers/spikes/2026-10-09-mcp-repo-scope-verification.md, Q2):
    # the key is plugin:<plugin-name>:<server>, no @marketplace suffix. Registry ids are name@marketplace.
    assert server_key({"name": "railway", "source": "railway@claude-plugins-official"}) == "plugin:railway:railway"
