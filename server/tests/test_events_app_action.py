"""Task 2.4 (spec §4.4/§4.8): APP_ACTION tool kind + PermissionRequest.always_allowed."""
from server.sessions.events import PermissionRequest, ToolKind, tool_kind


def test_app_action_kind():
    assert tool_kind("mcp__ccswitch__archive_session") is ToolKind.APP_ACTION
    assert tool_kind("mcp__other__x") is ToolKind.MCP


def test_permission_request_always_allowed_on_wire():
    ev = PermissionRequest("p", "t", "mcp__ccswitch__delete_session", ToolKind.APP_ACTION, {},
                            always_allowed=False)
    assert ev.to_wire()["alwaysAllowed"] is False
    assert PermissionRequest("p", "t", "Bash", ToolKind.BASH, {}).to_wire()["alwaysAllowed"] is True
