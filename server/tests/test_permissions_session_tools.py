"""report_feedback / session_context resolve silently in EVERY mode — they are READ-class
on the shared registry (session tools spec §3.2)."""
from __future__ import annotations

import pytest

import server.sessions.tools  # noqa: F401 - registers the surface
from server.mcp_spec import MCP_PREFIX
from server.sessions.permissions import PermissionRegistry


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["interactive", "default", "acceptEdits", "full-auto"])
@pytest.mark.parametrize("tool", [MCP_PREFIX + "report_feedback",
                                 MCP_PREFIX + "session_context"])
async def test_session_tools_never_raise_a_card(mode, tool):
    reg = PermissionRegistry()
    pid, fut, _ = await reg.request("s1", "t1", tool, {"category": "prompt"}, mode=mode,
                                    denylist=(), allowlist=True, identity="u")
    assert pid is None and fut.done() and fut.result() is True
