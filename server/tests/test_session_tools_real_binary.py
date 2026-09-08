"""Constitution III: verify against the real `claude` binary. Gated on
CCSWITCH_TEST_CLAUDE_MCP=1 (needs the system claude + a logged-in account)."""
from __future__ import annotations

import json
import os

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("CCSWITCH_TEST_CLAUDE_MCP") != "1",
                                reason="set CCSWITCH_TEST_CLAUDE_MCP=1 to run against the real claude binary")


@pytest.mark.asyncio
async def test_project_mcp_server_and_our_server_coexist(tmp_path):
    """(a) a coding session with a project .mcp.json sees BOTH that server and our own
    `<prefix>session_context`; (b) calling report_feedback in `default` mode is
    verified as the registry resolves the call without a card — not that the SDK
    never invokes `can_use_tool` (it does, even for readOnlyHint tools; "no permission
    card" per spec §3.5b is our own PermissionRegistry decision, not the SDK's
    callback-invocation policy).

    Wire names come from `brand`, so this exercises the CURRENT server name rather
    than passing only via the dual-accept compat path."""
    from claude_agent_sdk import (AssistantMessage, ClaudeSDKClient, ClaudeAgentOptions,
                                  PermissionResultAllow, PermissionResultDeny, ToolUseBlock)
    from ccswitch import brand
    from server.mcp_spec import MCP_PREFIX
    from server.sessions.claude_bin import cli_path_kwargs
    from server.sessions.permissions import PermissionRegistry
    import server.sessions.tools  # noqa: F401 - registers the "session" surface
    from server.sessions.tools.context import SessionToolContext
    from server.sessions.tools.server import build_session_server
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {"echo": {
        "command": "python3", "args": ["-c", "import sys; sys.exit(0)"]}}}))
    ctx = SessionToolContext(session_id="real", identity="test", kind=None, engine="claude_code",
                             record=lambda: None, host="local")
    reg = PermissionRegistry()
    decisions: list[tuple[str, str | None]] = []
    async def can_use_tool(name, inp, ctx_=None):
        pid, fut, _ = await reg.request("real", "t1", name, inp, mode="default",
                                        denylist=(), allowlist=True, identity="test")
        decisions.append((name, pid))
        if pid is not None:
            reg.resolve(pid, "allow", "test")
        allowed = await fut
        return PermissionResultAllow() if allowed else PermissionResultDeny(message="denied")
    opts = ClaudeAgentOptions(
        cwd=str(tmp_path), permission_mode="default", can_use_tool=can_use_tool,
        settings=json.dumps({"enableAllProjectMcpServers": True}),
        mcp_servers={brand.MCP_SERVER_NAME: build_session_server(ctx)}, max_turns=6,
        disallowed_tools=["ToolSearch"],
        **cli_path_kwargs())
    used: list[str] = []
    async with ClaudeSDKClient(options=opts) as client:
        await client.query(f"Call the {brand.NAME} session_context tool, then call report_feedback with "
                           "category=other impact=minor title='Real-binary smoke check' body='ignore'. "
                           "Reply with the word done.")
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                used += [b.name for b in msg.content if isinstance(b, ToolUseBlock)]
    assert MCP_PREFIX + "session_context" in used
    assert MCP_PREFIX + "report_feedback" in used
    carded = [n for n, pid in decisions if n.startswith(MCP_PREFIX) and pid is not None]
    assert not carded, carded
