"""Constitution III: verify against the real `claude` binary. Gated on
CCSWITCH_TEST_CLAUDE_MCP=1 (needs the system claude + a logged-in account)."""
from __future__ import annotations

import json
import os

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("CCSWITCH_TEST_CLAUDE_MCP") != "1",
                                reason="set CCSWITCH_TEST_CLAUDE_MCP=1 to run against the real claude binary")


@pytest.mark.asyncio
async def test_project_mcp_server_and_ccswitch_server_coexist(tmp_path):
    """(a) a coding session with a project .mcp.json sees BOTH that server and
    mcp__ccswitch__session_context; (b) calling report_feedback in `default` mode
    produces no permission prompt."""
    from claude_agent_sdk import AssistantMessage, ClaudeSDKClient, ClaudeAgentOptions, ToolUseBlock
    from server.sessions.claude_bin import cli_path_kwargs
    from server.sessions.tools.context import SessionToolContext
    from server.sessions.tools.server import build_session_server
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {"echo": {
        "command": "python3", "args": ["-c", "import sys; sys.exit(0)"]}}}))
    ctx = SessionToolContext(session_id="real", identity="test", kind=None, engine="claude_code",
                             record=lambda: None, host="local")
    prompts: list[str] = []
    async def can_use_tool(name, inp, ctx_=None):
        from claude_agent_sdk import PermissionResultAllow
        prompts.append(name)
        return PermissionResultAllow()
    opts = ClaudeAgentOptions(
        cwd=str(tmp_path), permission_mode="default", can_use_tool=can_use_tool,
        settings=json.dumps({"enableAllProjectMcpServers": True}),
        mcp_servers={"ccswitch": build_session_server(ctx)}, max_turns=6,
        disallowed_tools=["ToolSearch"],
        **cli_path_kwargs())
    used: list[str] = []
    async with ClaudeSDKClient(options=opts) as client:
        await client.query("Call the ccswitch session_context tool, then call report_feedback with "
                           "category=other impact=minor title='Real-binary smoke check' body='ignore'. "
                           "Reply with the word done.")
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                used += [b.name for b in msg.content if isinstance(b, ToolUseBlock)]
    assert "mcp__ccswitch__session_context" in used
    assert "mcp__ccswitch__report_feedback" in used
    assert not [p for p in prompts if p.startswith("mcp__ccswitch__")], prompts
