"""A SIGNAL-class ccswitch MCP tool auto-allows with no permission card, in every
session mode — same no-card contract as READ (task-1-brief, marker-to-tool migration)."""
from __future__ import annotations

import pytest

from server.mcp_spec import ToolClass, ToolSpec, register_surface
from server.sessions.permissions import PermissionRegistry


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["interactive", "acceptEdits", "full-auto", "bypassPermissions"])
async def test_signal_tool_auto_allows_with_no_card_in_every_mode(mode):
    register_surface("test-signal", {"sig_probe": ToolSpec("sig_probe", ToolClass.SIGNAL, "Probe")})
    try:
        reg = PermissionRegistry()
        pid, fut, _ = await reg.request("s1", "t1", "mcp__viberpit__sig_probe", {}, mode=mode,
                                        denylist=(), allowlist=True, identity="u")
        assert pid is None, "a SIGNAL tool must never create a pending permission card"
        assert fut.done() and fut.result() is True
    finally:
        from server import mcp_spec
        mcp_spec._SURFACES.pop("test-signal", None)
