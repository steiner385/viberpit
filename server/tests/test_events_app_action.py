"""Task 2.4 (spec §4.4/§4.8): APP_ACTION tool kind + PermissionRequest.always_allowed."""
from ccswitch.brand import MCP_TOOL_PREFIX as MCP_PREFIX
from server.sessions.events import PermissionRequest, ToolKind, tool_kind


def test_app_action_kind():
    assert tool_kind(MCP_PREFIX + "archive_session") is ToolKind.APP_ACTION
    assert tool_kind("mcp__other__x") is ToolKind.MCP


def test_permission_request_always_allowed_on_wire():
    ev = PermissionRequest("p", "t", MCP_PREFIX + "delete_session", ToolKind.APP_ACTION, {},
                            always_allowed=False)
    assert ev.to_wire()["alwaysAllowed"] is False
    assert PermissionRequest("p", "t", "Bash", ToolKind.BASH, {}).to_wire()["alwaysAllowed"] is True


def test_permission_request_wire_has_items():
    p = PermissionRequest("p", "t", MCP_PREFIX + "archive_sessions", ToolKind.APP_ACTION, {},
                          items=["A", "B"])
    assert p.to_wire()["items"] == ["A", "B"]
    # None for a tool with no items (the common case).
    assert PermissionRequest("p", "t", "Bash", ToolKind.BASH, {}).to_wire()["items"] is None
