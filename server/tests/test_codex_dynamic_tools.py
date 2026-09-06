"""`thread/start` `dynamicTools` and the `item/tool/call` callback (#1321).

The transport half of the Codex assistant overlay. Three layers, one per file it
touches: the pinned schema (what may go on the wire), the protocol client (who
answers the callback, and what happens when nobody can) and the runner manager
(which thread's tool table a call reaches).

The property under test throughout is that **no failure path fabricates a tool
result**. An approval that cannot be serviced is safe to leave unanswered; a tool
call that cannot be serviced must be reported as failed, because an invented
answer is one the model will reason from.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.codex import protocol, schema
from server.tests.fakes.fake_codex_app_server import FakeCodexAppServer

TOOL_CALL = schema.METHOD_ITEM_TOOL_CALL


def _spec(name: str = "list_sessions") -> schema.FunctionDynamicToolSpec:
    return schema.FunctionDynamicToolSpec(
        name=name, description="List sessions",
        input_schema={"type": "object", "properties": {}, "required": []})


def _params(**over: Any) -> dict[str, Any]:
    body = {"threadId": "t-1", "turnId": "turn-1", "callId": "call-1",
            "tool": "list_sessions", "arguments": {}}
    body.update(over)
    return body


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_item_tool_call_is_a_supported_server_request():
    # It has always been in DECLARED_SERVER_REQUESTS (the binary's full surface);
    # what changed is that ccswitch now ANSWERS it. Before this, protocol.py
    # replied "unsupported server request" and no dynamic tool could ever run.
    assert TOOL_CALL in schema.SUPPORTED_SERVER_REQUESTS
    assert TOOL_CALL in schema.DECLARED_SERVER_REQUESTS


def test_dynamic_tools_ride_thread_start():
    wire = schema.ThreadStartParams(dynamic_tools=(_spec(),)).to_wire()
    assert wire["dynamicTools"] == [{
        "type": "function", "name": "list_sessions", "description": "List sessions",
        "inputSchema": {"type": "object", "properties": {}, "required": []}}]


def test_no_dynamic_tools_emits_no_field():
    # A coding thread must look exactly as it did before: an empty list would
    # declare "this thread has a host tool surface, and it is empty".
    assert "dynamicTools" not in schema.ThreadStartParams(cwd="/tmp").to_wire()


def test_a_duplicate_tool_name_is_refused():
    with pytest.raises(schema.CodexSchemaError):
        schema.ThreadStartParams(dynamic_tools=(_spec(), _spec()))


@pytest.mark.parametrize("kwargs", [
    {"name": "", "description": "d", "input_schema": {}},
    {"name": "n", "description": "", "input_schema": {}},
    {"name": "n", "description": "d", "input_schema": "not-an-object"},
])
def test_a_malformed_spec_is_refused(kwargs):
    with pytest.raises(schema.CodexSchemaError):
        schema.FunctionDynamicToolSpec(**kwargs)


def test_base_and_developer_instructions_cannot_both_be_sent():
    # One replaces the app-server's agent prompt, the other appends to it.
    # Sending both would let the app-server pick which wins.
    with pytest.raises(schema.CodexSchemaError):
        schema.ThreadStartParams(base_instructions="a", developer_instructions="b")


def test_thread_resume_has_no_dynamic_tools_field():
    # The vendor boundary the "assistant threads are never resumed" rule rests
    # on. If a future codex-cli grows the field, this test fails and the rule can
    # be revisited deliberately rather than by accident.
    assert "dynamicTools" not in schema.ThreadResumeParams(thread_id="t").to_wire()
    assert not any(f.name == "dynamic_tools"
                   for f in schema.ThreadResumeParams.__dataclass_fields__.values())


def test_tool_call_params_parse():
    parsed = schema.parse_dynamic_tool_call_params(_params(arguments={"status": "done"}))
    assert (parsed.thread_id, parsed.turn_id, parsed.tool) == ("t-1", "turn-1",
                                                               "list_sessions")
    assert parsed.arguments == {"status": "done"}


def test_non_object_arguments_reach_the_tool_rather_than_failing_the_turn():
    # `arguments` is `true` (any JSON) upstream. A model that sends a list must
    # get a TOOL error from the overlay, not a protocol error that kills the turn.
    assert schema.parse_dynamic_tool_call_params(_params(arguments=[1, 2])).arguments == [1, 2]


@pytest.mark.parametrize("missing", ["threadId", "turnId", "callId", "tool"])
def test_missing_required_params_are_refused(missing):
    body = _params()
    del body[missing]
    with pytest.raises(schema.CodexSchemaError):
        schema.parse_dynamic_tool_call_params(body)


def test_the_response_round_trips():
    response = schema.DynamicToolCallResponse(
        success=True, content_items=(schema.DynamicToolCallContentItem(text="hi"),))
    wire = response.to_wire()
    assert wire == {"success": True, "contentItems": [{"type": "inputText", "text": "hi"}]}
    assert schema.parse_dynamic_tool_call_response(wire) == response


def test_a_non_text_content_item_is_refused_on_the_way_out():
    with pytest.raises(schema.CodexSchemaError):
        schema.parse_dynamic_tool_call_response(
            {"success": True, "contentItems": [{"type": "inputImage", "imageUrl": "u"}]})


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


async def _client(server: FakeCodexAppServer, **kw: Any) -> protocol.CodexAppServerClient:
    client = protocol.CodexAppServerClient(
        server.transport, external_auth_enabled=False, request_timeout_s=5.0, **kw)
    await client.start()
    await asyncio.gather(client.initialize(), server.serve_initialize())
    return client


@pytest.mark.asyncio
async def test_a_tool_call_reaches_the_installed_handler():
    seen: list[schema.DynamicToolCallParams] = []

    async def handler(params):
        seen.append(params)
        return schema.DynamicToolCallResponse(
            success=True,
            content_items=(schema.DynamicToolCallContentItem(text="[]"),))

    async with FakeCodexAppServer() as server:
        client = await _client(server, dynamic_tool_handler=handler)
        try:
            rid = server.request(TOOL_CALL, _params())
            reply = await server.receive_reply(rid)
        finally:
            await client.aclose()
    assert reply["result"] == {"success": True,
                               "contentItems": [{"type": "inputText", "text": "[]"}]}
    assert seen[0].tool == "list_sessions"


@pytest.mark.asyncio
async def test_without_a_handler_the_call_fails_closed():
    # A coding thread installs no handler. `-32601` is the ONLY safe answer: the
    # model is told the tool did not run, rather than handed an empty result it
    # would read as "there are no sessions".
    async with FakeCodexAppServer() as server:
        client = await _client(server)
        try:
            rid = server.request(TOOL_CALL, _params())
            reply = await server.receive_reply(rid)
        finally:
            await client.aclose()
    assert reply["error"]["code"] == protocol.JSONRPC_METHOD_NOT_FOUND


@pytest.mark.asyncio
async def test_malformed_params_are_invalid_params():
    async def handler(_params):  # pragma: no cover - must never be reached
        raise AssertionError("the handler must not see malformed params")

    async with FakeCodexAppServer() as server:
        client = await _client(server, dynamic_tool_handler=handler)
        try:
            rid = server.request(TOOL_CALL, {"threadId": "t-1"})
            reply = await server.receive_reply(rid)
        finally:
            await client.aclose()
    assert reply["error"]["code"] == protocol.JSONRPC_INVALID_PARAMS


@pytest.mark.asyncio
async def test_a_raising_handler_is_an_internal_error_not_a_result():
    async def handler(_params):
        raise RuntimeError("boom")

    async with FakeCodexAppServer() as server:
        client = await _client(server, dynamic_tool_handler=handler)
        try:
            rid = server.request(TOOL_CALL, _params())
            reply = await server.receive_reply(rid)
        finally:
            await client.aclose()
    assert reply["error"]["code"] == protocol.JSONRPC_INTERNAL_ERROR
    assert "boom" not in str(reply)


@pytest.mark.asyncio
async def test_a_handler_answering_an_unrenderable_item_is_refused_before_the_wire():
    async def handler(_params):
        return schema.DynamicToolCallResponse(
            success=True,
            content_items=(schema.DynamicToolCallContentItem(text=object()),))  # type: ignore[arg-type]

    async with FakeCodexAppServer() as server:
        client = await _client(server, dynamic_tool_handler=handler)
        try:
            rid = server.request(TOOL_CALL, _params())
            reply = await server.receive_reply(rid)
        finally:
            await client.aclose()
    assert reply["error"]["code"] == protocol.JSONRPC_INTERNAL_ERROR


# ---------------------------------------------------------------------------
# Manager routing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_call_reaches_only_the_thread_that_declared_the_tool(tmp_path):
    from server.tests.test_codex_driver import ACCOUNT, Harness

    harness = Harness(tmp_path)
    await harness.boot(start=False)
    manager = harness.manager
    seen: list[str] = []

    async def sink(params):
        seen.append(params.thread_id)
        return schema.DynamicToolCallResponse(success=True)

    manager.register_thread(ACCOUNT, "thread-a", lambda _n: None, dynamic_tools=sink)
    await manager._on_dynamic_tool(
        ACCOUNT, schema.parse_dynamic_tool_call_params(_params(threadId="thread-a")))
    assert seen == ["thread-a"]

    # A different thread of the SAME account has no sink: an account-wide handler
    # would let one session's model run another session's tool table.
    with pytest.raises(Exception):
        await manager._on_dynamic_tool(
            ACCOUNT, schema.parse_dynamic_tool_call_params(_params(threadId="thread-b")))
    assert manager.unrouted_dynamic_tools == 1
    await manager.stop_all()


@pytest.mark.asyncio
async def test_unregistering_a_thread_drops_its_tool_sink(tmp_path):
    from server.tests.test_codex_driver import ACCOUNT, Harness

    harness = Harness(tmp_path)
    await harness.boot(start=False)
    manager = harness.manager

    async def sink(_params):  # pragma: no cover - must never be reached
        raise AssertionError("a torn-down thread must not run tools")

    manager.register_thread(ACCOUNT, "thread-a", lambda _n: None, dynamic_tools=sink)
    manager.unregister_thread(ACCOUNT, "thread-a")
    with pytest.raises(Exception):
        await manager._on_dynamic_tool(
            ACCOUNT, schema.parse_dynamic_tool_call_params(_params(threadId="thread-a")))
    await manager.stop_all()


@pytest.mark.asyncio
async def test_a_slow_tool_does_not_block_the_reader(tmp_path):
    """A tool that takes a while must not stall the connection's other traffic."""
    release = asyncio.Event()

    async def handler(_params):
        await release.wait()
        return schema.DynamicToolCallResponse(success=True)

    async with FakeCodexAppServer() as server:
        client = await _client(server, dynamic_tool_handler=handler)
        try:
            rid = server.request(TOOL_CALL, _params())
            await asyncio.sleep(0.01)
            assert client.inflight_server_request_count == 1
            release.set()
            reply = await server.receive_reply(rid)
        finally:
            await client.aclose()
    assert reply["result"]["success"] is True
