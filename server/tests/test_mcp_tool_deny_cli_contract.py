"""D-16b contract test against the INSTALLED ``claude`` binary (constitution I/III).

The Claude driver turns MCP tools off with flag-layer deny rules: it sends
``apply_flag_settings {permissions: <flag_settings.compose_permissions(...)>}`` after
``initialize`` and resends the whole object on every live toggle. This pins, against
the binary, the D-16 spike's §2.1/§2.3 findings ViberPit relies on:

- a live deny of ``mcp__probe__tool_b`` removes it from ``get_context_usage.mcpTools``
  (the effective tool pool), not merely at call time;
- a later ``deny: []`` restores it — the object is replaced, never a floor;
- ``mcp_status`` still lists every tool while one is denied (the stable list the
  switches render from), and ``sanitize_statuses`` reads it.

The MCP server is a stdio JSON-RPC stub with three tools (the spike's probe server). The
harness is D-14's (``test_settings_cli_contract._Cli``): control requests only, never a
user message; a fresh ``CLAUDE_CONFIG_DIR`` and ``HOME`` and an env built from scratch,
so it holds no credential; ``ANTHROPIC_BASE_URL`` at a closed loopback port. Skipped when
no ``claude`` resolves (set ``VIBERPIT_CLAUDE_BIN`` under an isolated ``HOME``).
"""
from __future__ import annotations

import json
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from server.mcp_health.status import mcp_tool_rule, sanitize_statuses
from server.mcp_scope.resolve import McpToolRules
from server.sessions import flag_settings as fs
from server.tests.test_settings_cli_contract import _Cli, binary  # noqa: F401 (fixture)

pytestmark = pytest.mark.slow

TOOLS = ("tool_a", "tool_b", "tool_c")
#: A claude.ai-style connector name, which the CLI's En() normalises differently
#: (collapses `_` runs, trims the ends), and a tool name with a dot.
CONNECTOR = "claude.ai My__Conn"
_PROBE = """\
import json, sys
for line in sys.stdin:
    try:
        msg = json.loads(line)
    except ValueError:
        continue
    mid, meth = msg.get("id"), msg.get("method")
    if mid is None:
        continue
    if meth == "initialize":
        res = {"protocolVersion": msg["params"].get("protocolVersion", "2025-06-18"),
               "capabilities": {"tools": {}},
               "serverInfo": {"name": "probe", "version": "0.0.1"}}
    elif meth == "tools/list":
        res = {"tools": [{"name": n, "description": "probe " + n,
                          "annotations": {"readOnlyHint": n == "tool_a"},
                          "inputSchema": {"type": "object", "properties": {}}}
                         for n in %r + ("tool.d",)]}
    else:
        res = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": res}) + "\\n")
    sys.stdout.flush()
""" % (TOOLS,)


@pytest.fixture
def cli(binary: str, tmp_path: Path) -> Iterator[_Cli]:  # noqa: F811
    probe = tmp_path / "mcp_probe.py"
    probe.write_text(_PROBE)
    stdio = {"type": "stdio", "command": sys.executable, "args": ["-I", str(probe)]}
    config = {"mcpServers": {"probe": stdio, CONNECTOR: stdio}}
    c = _Cli(binary, ["--mcp-config", json.dumps(config)])
    try:
        c.ok({"subtype": "initialize", "hooks": None})
        yield c
    finally:
        c.close()


def _status(cli: _Cli) -> list:
    """``mcp_status`` once the probe server has connected (it starts ``pending``)."""
    deadline = time.monotonic() + 30
    while True:
        states = sanitize_statuses(cli.ok({"subtype": "mcp_status"}))
        mine = [s for s in states if s.name in ("probe", CONNECTOR)]
        if (len(mine) == 2 and all(s.status != "pending" for s in mine)
                or time.monotonic() > deadline):
            return states
        time.sleep(0.25)


def _pool(cli: _Cli) -> set[str]:
    usage = cli.ok({"subtype": "get_context_usage", "detail": "summary"})
    return {t["name"] for t in usage.get("mcpTools") or []}


def _send(cli: _Cli, rules: McpToolRules) -> None:
    # The driver's exact patch (ClaudeAgentDriver._send_permissions).
    cli.ok({"subtype": "apply_flag_settings",
            "settings": {fs.PERMISSIONS_KEY: fs.compose_permissions(rules)}})


def test_a_live_deny_leaves_the_pool_and_deny_empty_restores_it(cli: _Cli) -> None:
    [probe] = [s for s in _status(cli) if s.name == "probe"]
    assert probe.status == "connected" and probe.tools is not None
    assert [t.name for t in probe.tools] == [*TOOLS, "tool.d"]
    every = {mcp_tool_rule("probe", t) for t in TOOLS}
    assert every <= _pool(cli)

    off = McpToolRules(defaults={"probe": {"tool_b": False}})
    _send(cli, off)
    assert _pool(cli) & every == every - {"mcp__probe__tool_b"}
    # mcp_status still lists every tool: the switches' stable list.
    [probe] = [s for s in _status(cli) if s.name == "probe"]
    assert probe.tools is not None and [t.name for t in probe.tools] == [*TOOLS, "tool.d"]

    # Review Focus 1: the session re-enables it live — the whole object, deny: [].
    _send(cli, off.with_override("probe", "tool_b", True))
    assert every <= _pool(cli)


def test_a_new_object_replaces_the_old_one(cli: _Cli) -> None:
    _status(cli)
    every = {mcp_tool_rule("probe", t) for t in TOOLS}
    _send(cli, McpToolRules(defaults={"probe": {"tool_b": False}}))
    _send(cli, McpToolRules(defaults={"probe": {"tool_c": False}}))
    assert _pool(cli) & every == every - {"mcp__probe__tool_c"}


def test_rule_names_match_the_clis_tool_names(cli: _Cli) -> None:
    """``mcp_tool_rule`` spells names as the CLI does (its ``En``), including a claude.ai
    connector and a dotted tool name: each rule names a pool entry, and denying it
    removes exactly that entry."""
    _status(cli)
    pairs = [(srv, t) for srv in ("probe", CONNECTOR) for t in (*TOOLS, "tool.d")]
    rules = {mcp_tool_rule(srv, t) for srv, t in pairs}
    assert rules <= _pool(cli), sorted(_pool(cli))
    deny = McpToolRules(defaults={CONNECTOR: {"tool_a": False, "tool.d": False}})
    _send(cli, deny)
    assert _pool(cli) & rules == rules - {mcp_tool_rule(CONNECTOR, "tool_a"),
                                          mcp_tool_rule(CONNECTOR, "tool.d")}
