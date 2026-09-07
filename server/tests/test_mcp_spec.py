"""server/mcp_spec.py is THE home of the MCP registry primitives both surfaces share.

Wire names are built from ``mcp_spec.MCP_PREFIX`` (an alias for ``brand.MCP_TOOL_PREFIX``)
rather than hand-typed, so these tests pin the registry's behaviour and hold whichever
slug brand declares. ``brand.LEGACY_MCP_TOOL_PREFIX`` appears once, on purpose: the
dual-accept is a contract of ``annotation_for`` and it needs its own assertion.
"""
from __future__ import annotations

import pytest

from ccswitch import brand


def test_assistant_surface_is_registered_and_unchanged():
    from server import mcp_spec
    from server.assistant.tools import TOOL_SPECS
    assert mcp_spec.surfaces()["assistant"] is TOOL_SPECS
    assert mcp_spec.annotation_for(
        mcp_spec.MCP_PREFIX + "list_sessions").tool_class is mcp_spec.ToolClass.READ
    assert mcp_spec.annotation_for(
        mcp_spec.MCP_PREFIX + "delete_session").tool_class is mcp_spec.ToolClass.DESTRUCTIVE


def test_annotation_for_consults_every_surface():
    from server import mcp_spec
    specs = {"probe_tool": mcp_spec.ToolSpec("probe_tool", mcp_spec.ToolClass.READ, "Probe")}
    mcp_spec.register_surface("probe", specs)
    try:
        assert mcp_spec.annotation_for(
            mcp_spec.MCP_PREFIX + "probe_tool") is specs["probe_tool"]
        # ...under EITHER brand prefix: a pre-rebrand name in a replayed transcript or
        # a resumed thread must still resolve to its annotation, or the permission
        # registry downgrades a DESTRUCTIVE tool to a grantable mutation card.
        assert mcp_spec.annotation_for(
            brand.LEGACY_MCP_TOOL_PREFIX + "probe_tool") is specs["probe_tool"]
        assert mcp_spec.annotation_for("mcp__other__probe_tool") is None
        assert mcp_spec.annotation_for("Bash") is None
    finally:
        mcp_spec._SURFACES.pop("probe", None)


def test_duplicate_tool_name_across_surfaces_is_refused():
    from server import mcp_spec
    with pytest.raises(ValueError):
        mcp_spec.register_surface("dup", {"list_sessions": mcp_spec.ToolSpec(
            "list_sessions", mcp_spec.ToolClass.READ, "x")})


def test_assistant_module_reexports_the_primitives():
    from server import mcp_spec
    from server.assistant import tools
    assert tools.ToolClass is mcp_spec.ToolClass
    assert tools.ToolSpec is mcp_spec.ToolSpec
    assert tools.MCP_PREFIX == mcp_spec.MCP_PREFIX == brand.MCP_TOOL_PREFIX
    assert mcp_spec.SERVER_NAME == brand.MCP_SERVER_NAME
    assert mcp_spec.MCP_TOOL_PREFIXES == brand.MCP_TOOL_PREFIXES
    assert tools.annotation_for is mcp_spec.annotation_for
