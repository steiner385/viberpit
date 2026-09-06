"""`ccswitch mcp` — the stdio MCP server over the assistant's own tool table.

Drives the server through a REAL `mcp` client session over an in-memory stream pair,
so what is asserted is the wire surface an external agent sees (tool list, schemas,
`isError`), not the python objects behind it.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

import ccswitch
from ccswitch import assistant_mcp


def _api(handler, **kw):
    """A real AssistantApi whose HTTP layer is an httpx MockTransport."""
    return assistant_mcp.build_api(url="http://127.0.0.1:1", token="ccas2:x",
                                   transport=httpx.MockTransport(handler), **kw)


def _ok(_req):
    return httpx.Response(200, json=[])


async def _names(api, *, allow_mutations: bool) -> set[str]:
    server = assistant_mcp.build_mcp_server(api, allow_mutations=allow_mutations)
    async with create_connected_server_and_client_session(server) as client:
        return {t.name for t in (await client.list_tools()).tools}


@pytest.mark.asyncio
async def test_read_only_tool_table_by_default():
    names = await _names(_api(_ok), allow_mutations=False)
    from server.assistant.tools import TOOL_SPECS, ToolClass

    assert names, "the read-only table must not be empty"
    assert all(TOOL_SPECS[n].tool_class is ToolClass.READ for n in names)
    assert "list_sessions" in names and "report_gap" in names
    assert "archive_session" not in names and "delete_session" not in names


@pytest.mark.asyncio
async def test_allow_mutations_adds_mutating_tools():
    names = await _names(_api(_ok), allow_mutations=True)
    assert "archive_session" in names and "delete_session" in names
    assert "list_sessions" in names


@pytest.mark.asyncio
async def test_operator_tools_are_never_served():
    from server.assistant.tools import OPERATOR_TOOLS

    for allow in (False, True):
        names = await _names(_api(_ok), allow_mutations=allow)
        assert not (names & OPERATOR_TOOLS)


@pytest.mark.asyncio
async def test_schemas_are_real_json_schema_not_the_sdk_shorthand():
    """`get_session` declares `{"id": str}` — the SDK's shorthand, not JSON Schema.
    The served schema must be the CONVERTED form, or every client rejects it."""
    server = assistant_mcp.build_mcp_server(_api(_ok), allow_mutations=False)
    async with create_connected_server_and_client_session(server) as client:
        schemas = {t.name: t.inputSchema for t in (await client.list_tools()).tools}
    assert schemas["get_session"] == {
        "type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}


@pytest.mark.asyncio
async def test_call_round_trips_through_the_api():
    seen = {}

    def handler(req: httpx.Request):
        seen["path"] = req.url.path
        seen["auth"] = req.headers["authorization"]
        return httpx.Response(200, json=[{"id": "abc", "name": "Refactor auth",
                                          "status": "waiting_input", "repo": "o/r"}])

    server = assistant_mcp.build_mcp_server(_api(handler), allow_mutations=False)
    async with create_connected_server_and_client_session(server) as client:
        res = await client.call_tool("list_sessions", {})
    assert res.isError is False
    assert seen == {"path": "/api/sessions", "auth": "Bearer ccas2:x"}
    body = json.loads(res.content[0].text)
    assert body["data"][0]["id"] == "abc"


@pytest.mark.asyncio
async def test_api_error_round_trips_as_an_error_result():
    server = assistant_mcp.build_mcp_server(
        _api(lambda r: httpx.Response(404, json={"error": {"code": "NOT_FOUND",
                                                           "message": "no such session"}})),
        allow_mutations=False)
    async with create_connected_server_and_client_session(server) as client:
        res = await client.call_tool("get_session", {"id": "nope"})
    assert res.isError is True
    assert "no such session" in res.content[0].text


@pytest.mark.asyncio
async def test_calling_a_withheld_mutation_is_an_error():
    server = assistant_mcp.build_mcp_server(_api(_ok), allow_mutations=False)
    async with create_connected_server_and_client_session(server) as client:
        res = await client.call_tool("archive_session", {"id": "abc"})
    assert res.isError is True


def test_cli_requires_a_token(monkeypatch, capsys):
    from ccswitch import cli

    monkeypatch.delenv("CCSWITCH_ASSISTANT_TOKEN", raising=False)
    assert cli.main(["mcp", "--url", "http://127.0.0.1:1"]) == 2
    err = capsys.readouterr().err
    assert "CCSWITCH_ASSISTANT_TOKEN" in err


def test_cli_reads_the_named_token_env_var(monkeypatch, capsys):
    """`--token-env` names the variable; its absence is reported by NAME."""
    from ccswitch import cli

    monkeypatch.delenv("MY_TOKEN", raising=False)
    assert cli.main(["mcp", "--token-env", "MY_TOKEN"]) == 2
    assert "MY_TOKEN" in capsys.readouterr().err


def test_stdio_subprocess_serves_a_real_jsonrpc_session():
    """Spec §9's live-smoke row for this task: a real MCP client speaking JSON-RPC to a
    real `ccswitch mcp` PROCESS over pipes.

    The in-memory tests above cannot catch the failure this one exists for — a stray
    `print()` on stdout corrupting the protocol stream, which is invisible until a
    client tries to parse it. `tools/list` needs no edge, so the unreachable `--url`
    never matters.
    """
    src_root = str(Path(ccswitch.__file__).resolve().parents[1])
    repo_root = str(Path(__file__).resolve().parents[1])   # `server` package lives here
    env = {**os.environ,
           "PYTHONPATH": os.pathsep.join([src_root, repo_root]),
           "CCSWITCH_ASSISTANT_TOKEN": "ccas2:smoke"}
    # Every request is written up front and stdin closed, so there is no interleaved
    # read to deadlock on: the server answers in order, then exits on EOF.
    stdin = "\n".join(json.dumps(m) for m in [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "ccswitch-smoke", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]) + "\n"

    proc = subprocess.run(
        [sys.executable, "-c", "import sys; from ccswitch.cli import main; sys.exit(main())",
         "mcp", "--url", "http://127.0.0.1:1"],
        input=stdin, capture_output=True, text=True, env=env, timeout=120)

    assert proc.returncode == 0, proc.stderr
    frames = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    by_id = {f.get("id"): f for f in frames}
    assert by_id[1]["result"]["serverInfo"]["name"] == "ccswitch"
    tools = {t["name"]: t for t in by_id[2]["result"]["tools"]}
    assert "list_sessions" in tools and "archive_session" not in tools
    assert tools["get_session"]["inputSchema"]["properties"] == {"id": {"type": "string"}}
    assert tools["get_session"]["annotations"]["readOnlyHint"] is True
    assert "ccswitch mcp: serving read-only tools" in proc.stderr


def test_cli_serves_and_banners_on_stderr(monkeypatch, capsys):
    """The banner must never touch stdout: stdout IS the JSON-RPC channel."""
    from ccswitch import cli

    served = {}

    async def fake_serve(api, *, allow_mutations):
        served["allow_mutations"] = allow_mutations
        served["base_url"] = str(api._client.base_url)
        await api.aclose()

    monkeypatch.setenv("CCSWITCH_ASSISTANT_TOKEN", "ccas2:tok")
    monkeypatch.setattr(assistant_mcp, "serve", fake_serve)
    assert cli.main(["mcp", "--url", "http://edge:8787", "--allow-mutations"]) == 0

    assert served == {"allow_mutations": True, "base_url": "http://edge:8787"}
    out = capsys.readouterr()
    assert out.out == ""
    assert "mutations" in out.err.lower()
