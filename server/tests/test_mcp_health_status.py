from server.mcp_health.status import McpServerState, redact_error, sanitize_statuses
from server.mcp_spec import SERVER_NAME
from server.sessions import events as ev


def test_sanitize_drops_config_and_counts_tools():
    raw = {"mcpServers": [{"name": "notion", "status": "connected", "scope": "user",
                           "config": {"type": "http", "headers": {"Authorization": "Bearer s3cret"}},
                           "tools": [{"name": "a"}, {"name": "b"}]}]}
    [s] = sanitize_statuses(raw)
    assert s == McpServerState("notion", "connected", None, 2, "user")
    assert "s3cret" not in repr(s) and "s3cret" not in str(s.to_wire())


def test_sanitize_filters_internal_server_by_name_only():
    raw = {"mcpServers": [{"name": SERVER_NAME, "status": "connected"},
                          {"name": "plugin:p:s", "status": "connected", "scope": "dynamic"},
                          {"name": "gh", "status": "failed", "error": "boom"}]}
    assert [s.name for s in sanitize_statuses(raw)] == ["plugin:p:s", "gh"]


def test_unknown_status_kept_verbatim():
    [s] = sanitize_statuses({"mcpServers": [{"name": "x", "status": "connecting"}]})
    assert s.status == "connecting"


def test_redact_error_strips_query_and_truncates():
    out = redact_error("GET https://h/p?token=abc&x=1 failed " + "y" * 900)
    assert "abc" not in out and "?<redacted>" in out and len(out) <= 500


def test_sanitize_tolerates_garbage():
    assert sanitize_statuses(None) == [] and sanitize_statuses({"mcpServers": [1, {"status": "x"}]}) == []


def test_event_wire_shape():
    e = ev.McpStatus(servers=[McpServerState("n", "needs-auth", None, None, "user")])
    assert e.to_wire() == {"type": "mcpStatus", "servers": [
        {"name": "n", "status": "needs-auth", "error": None, "toolCount": None, "scope": "user"}]}
