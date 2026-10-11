"""Roadmap delegation remains behind the native session permission boundary."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from server.codex import schema
from server.mcp_spec import MCP_PREFIX, ToolClass, annotation_for
from server.sessions.permissions import PermissionRegistry
from server.sessions.tools.codex import CodexSessionTools
from server.sessions.tools.context import SessionToolContext
from server.tests.test_codex_driver import Harness, SESSION, THREAD

READS = ("roadmap_needs_you", "roadmap_item")
MUTATIONS = ("roadmap_resolve", "roadmap_hold", "roadmap_advance")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["interactive", "default", "acceptEdits", "full-auto"])
@pytest.mark.parametrize("name", READS + MUTATIONS)
async def test_roadmap_permission_class_is_enforced_in_every_mode(mode, name):
    annotation = annotation_for(MCP_PREFIX + name)
    assert annotation is not None
    assert annotation.tool_class == (ToolClass.READ if name in READS else ToolClass.MUTATION)
    registry = PermissionRegistry()
    pid, future, _ = await registry.request(
        SESSION, "turn-1", MCP_PREFIX + name, {}, mode=mode,
        denylist=(), identity="user@example.com")
    if name in READS:
        assert pid is None and future.result() is True
    else:
        assert pid is not None and not future.done()
        registry.resolve(pid, "deny", "user@example.com")
        assert await future is False


@pytest.mark.asyncio
@pytest.mark.parametrize("name", MUTATIONS)
@pytest.mark.parametrize("choice", ["allow", "deny"])
async def test_codex_roadmap_handler_cannot_run_before_permission(tmp_path, name, choice):
    record = SimpleNamespace(permission_mode="full-auto", origin=None)
    context = SessionToolContext(
        session_id=SESSION, identity="user@example.com", kind=None, engine="codex",
        record=lambda: record, signalling_tools=True)
    tools = CodexSessionTools.build(context)
    assert tools.declares(name)
    calls = []

    async def handler(arguments):
        calls.append(arguments)
        return {"content": [{"type": "text", "text": "delegation executed"}]}

    tools = replace(tools, handlers={**tools.handlers, name: handler})
    harness = Harness(tmp_path)
    try:
        driver = await harness.boot(mode="full-auto", session_tools=tools)
        arguments = {"proposal_id": "pending-proposal", "decision": "accept"}
        request_id = harness.server.request(schema.METHOD_ITEM_TOOL_CALL, {
            "threadId": THREAD, "turnId": "turn-1", "callId": "call-1",
            "tool": name, "arguments": arguments})
        async with asyncio.timeout(5):
            while not harness.registry.pending(SESSION):
                await asyncio.sleep(0)
        assert calls == []
        pending = harness.registry.pending(SESSION)
        assert pending[0]["tool"] == MCP_PREFIX + name
        driver.resolve_permission(pending[0]["permId"], choice, "user@example.com")
        reply = await harness.server.receive_reply(request_id)
        assert reply["result"]["success"] is (choice == "allow")
        assert calls == ([arguments] if choice == "allow" else [])
    finally:
        await harness.aclose()


@pytest.mark.asyncio
async def test_native_codex_accepts_roadmap_tool_schemas(tmp_path):
    """Opt-in real binary check; isolated home, no credentials or model turn."""
    import os
    import shutil
    import subprocess

    from server.codex.process import CodexProcess, build_child_env, ensure_codex_home
    from server.codex.protocol import CodexAppServerClient

    if os.environ.get("VIBERPIT_TEST_CODEX_ROADMAP") != "1":
        pytest.skip("set VIBERPIT_TEST_CODEX_ROADMAP=1 for native tool registration")
    binary = shutil.which(os.environ.get("VIBERPIT_CODEX_BIN", "codex"))
    assert binary, "the pinned Codex binary must be installed"
    home = ensure_codex_home(tmp_path / "runtime", "roadmap-smoke")
    version = subprocess.run([binary, "--version"], check=True, capture_output=True,
                             text=True, env=build_child_env(home), timeout=15)
    assert version.stdout.strip() == schema.PINNED_CLI_VERSION
    context = SessionToolContext(
        session_id="smoke", identity="smoke@example.invalid", kind=None, engine="codex",
        record=lambda: SimpleNamespace(permission_mode="full-auto"), signalling_tools=True)
    tools = CodexSessionTools.build(context)
    assert set(READS + MUTATIONS) <= {tool.name for tool in tools.dynamic_tools}
    process = CodexProcess(binary, home)
    await process.start()
    # Dynamic tools are an experimental vendor surface, already gated by the
    # production capability probe. Registration itself needs no authentication.
    client = CodexAppServerClient(process.transport, external_auth_enabled=True)
    try:
        await client.start()
        await client.initialize()
        started = await client.start_thread(schema.ThreadStartParams(
            cwd=str(tmp_path), sandbox="read-only", approval_policy="untrusted",
            dynamic_tools=tools.dynamic_tools))
        assert started.thread.id
        assert client.counters.unknown_server_requests == 0
        assert client.counters.unhandled_server_requests == 0
    finally:
        await client.aclose()
        await process.stop()
    assert not (home / "auth.json").exists()
