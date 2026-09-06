"""server/mcp_spec.py is THE home of the MCP registry primitives both surfaces share."""
from __future__ import annotations

import pytest


def test_assistant_surface_is_registered_and_unchanged():
    from server import mcp_spec
    from server.assistant.tools import TOOL_SPECS
    assert mcp_spec.surfaces()["assistant"] is TOOL_SPECS
    assert mcp_spec.annotation_for("mcp__ccswitch__list_sessions").tool_class is mcp_spec.ToolClass.READ
    assert mcp_spec.annotation_for("mcp__ccswitch__delete_session").tool_class is mcp_spec.ToolClass.DESTRUCTIVE


def test_annotation_for_consults_every_surface():
    from server import mcp_spec
    specs = {"probe_tool": mcp_spec.ToolSpec("probe_tool", mcp_spec.ToolClass.READ, "Probe")}
    mcp_spec.register_surface("probe", specs)
    try:
        assert mcp_spec.annotation_for("mcp__ccswitch__probe_tool") is specs["probe_tool"]
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
    assert tools.MCP_PREFIX == mcp_spec.MCP_PREFIX == "mcp__ccswitch__"
    assert tools.annotation_for is mcp_spec.annotation_for
