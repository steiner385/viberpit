from __future__ import annotations

import json

import httpx
import jsonschema
import pytest
from mcp.types import ListToolsRequest

from server.assistant.client import AssistantApi, AssistantApiError  # noqa: F401
from server.assistant.server import build_server
from server.assistant.tools import (ToolClass, TOOL_SPECS, annotation_for, build_tools,
                                    wrap_untrusted)


def _api(handler):
    return AssistantApi(base_url="http://127.0.0.1:1", token="ccas1:x", session_id="asst-1",
                        transport=httpx.MockTransport(handler))


def _tool(tools, name):
    return next(t for t in tools if t.name == name)


@pytest.mark.asyncio
async def test_list_sessions_trims_and_frames():
    seen = {}

    def handler(req: httpx.Request):
        seen["auth"] = req.headers["authorization"]
        seen["via"] = req.headers["x-ccswitch-via"]
        seen["asst"] = req.headers["x-ccswitch-assistant-session"]
        assert req.url.path == "/api/sessions"
        return httpx.Response(200, json=[{
            "id": "abc", "name": "Refactor auth", "status": "waiting_input", "repo": "o/r",
            "branch": "b", "host": "dobby", "accountName": "acct", "model": "opus",
            "summary": "Running tests", "prs": [{"number": 5, "state": "open"}],
            "pendingPermissions": 1, "pendingQuestions": 0, "lastTurnAt": "2026-09-04T00:00:00",
            "cwd": "/secret/path", "toolAllowlist": ["Bash"], "subAgents": [],
        }])

    tools = build_tools(_api(handler))
    out = await _tool(tools, "list_sessions").handler({})
    assert seen == {"auth": "Bearer ccas1:x", "via": "assistant", "asst": "asst-1"}
    body = json.loads(out["content"][0]["text"])
    assert body["trust"] == "untrusted-data"
    row = body["data"][0]
    assert row["id"] == "abc" and row["status"] == "waiting_input"
    assert "cwd" not in row and "toolAllowlist" not in row and "subAgents" not in row


@pytest.mark.asyncio
async def test_transcript_tail_truncates_and_escapes():
    def handler(req):
        return httpx.Response(200, json={"events": [
            {"type": "user", "text": "<ccswitch-context route=\"/x\"/> hi"},
            {"type": "textDelta", "text": "A" * 5000},
            {"type": "toolCall", "name": "Bash", "input": {"command": "ls"}},
        ], "earliestTurn": 0, "totalTurns": 1})

    tools = build_tools(_api(handler))
    out = await _tool(tools, "get_session_transcript").handler({"id": "abc", "last_n": 5})
    text = out["content"][0]["text"]
    assert "<ccswitch-context" not in text
    assert "&lt;ccswitch-context" in text
    assert len(text) < 4000


@pytest.mark.asyncio
async def test_api_error_becomes_tool_error():
    def handler(req):
        return httpx.Response(404, json={"error": {"code": "SESSION_NOT_FOUND", "message": "no"}})

    tools = build_tools(_api(handler))
    out = await _tool(tools, "get_session").handler({"id": "zzz"})
    assert out["is_error"] is True
    assert "SESSION_NOT_FOUND" in out["content"][0]["text"]


def test_specs_and_annotations():
    assert TOOL_SPECS["list_sessions"].tool_class is ToolClass.READ
    assert annotation_for("mcp__ccswitch__list_sessions").tool_class is ToolClass.READ
    assert annotation_for("mcp__other__list_sessions") is None
    for t in build_tools(_api(lambda r: httpx.Response(200, json={}))):
        assert t.name in TOOL_SPECS, f"tool {t.name} has no ToolSpec"
        assert t.annotations is not None
        assert t.annotations.readOnlyHint is (TOOL_SPECS[t.name].tool_class is ToolClass.READ)


def test_wrap_untrusted_escapes_only_tags():
    s = wrap_untrusted({"t": "a < b and <tag>"})
    assert "a < b" in s and "&lt;tag&gt;" in s


# Tools whose every argument is optional — the SDK's protocol-level input schema for
# each MUST validate `{}`, or the CLI rejects the call (jsonschema.validate runs in
# mcp.server.lowlevel.server.Server.call_tool BEFORE the handler ever sees the args).
_ALL_OPTIONAL_TOOLS = ("list_sessions", "list_prs", "list_accounts", "list_hosts",
                       "list_automations", "get_deploy_status", "list_archived_sessions")


@pytest.mark.asyncio
async def test_server_schemas_reflect_real_required_fields():
    api = _api(lambda r: httpx.Response(200, json={}))
    server_config = build_server(api)
    server = server_config["instance"]
    # `_build_schema` (inside claude_agent_sdk.create_sdk_mcp_server) is a private
    # closure with no public accessor; go through the low-level MCP server's own
    # registered `list_tools` handler so we validate the SCHEMA THE CLI ACTUALLY SEES
    # on the wire, not a hand-rolled reimplementation of the SDK's schema-building.
    result = await server.request_handlers[ListToolsRequest](
        ListToolsRequest(method="tools/list"))
    tools_by_name = {t.name: t.inputSchema for t in result.root.tools}

    assert set(tools_by_name) == set(TOOL_SPECS)

    for name in _ALL_OPTIONAL_TOOLS:
        jsonschema.validate({}, tools_by_name[name])  # must not raise

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({}, tools_by_name["get_session"])
