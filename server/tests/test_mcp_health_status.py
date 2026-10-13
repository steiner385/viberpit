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
        {"name": "n", "status": "needs-auth", "error": None, "toolCount": None, "scope": "user",
         "auth": None}]}


# --- Truthful auth state (spec 2026-10-13 "Truthful status") ---------------------------

NOW = 1_800_000_000_000          # ms


def _raw(*servers):
    return {"mcpServers": [{"name": n, "status": st} for n, st in servers]}


def _bundle(name, token="tok", expires=None, url="https://h/mcp"):
    b = {"serverName": name, "serverUrl": url, "accessToken": token, "clientId": "c"}
    if expires is not None:
        b["expiresAt"] = expires
    return b


def test_server_without_a_bundle_is_not_oauth():
    creds = {"other|1": _bundle("other")}
    [s] = sanitize_statuses(_raw(("gh", "connected")), credentials=creds, now_ms=NOW)
    assert (s.status, s.error, s.auth) == ("connected", None, None)


def test_usable_bundle_keeps_cli_status_and_marks_oauth():
    creds = {"aws|1": _bundle("aws", expires=NOW + 60_000)}
    [s] = sanitize_statuses({"mcpServers": [{"name": "aws", "status": "failed", "error": "boom"}]},
                            credentials=creds, now_ms=NOW)
    assert (s.status, s.error, s.auth) == ("failed", "boom", "oauth")


def test_bundle_without_expiry_is_usable():
    [s] = sanitize_statuses(_raw(("aws", "connected")), credentials={"aws|1": _bundle("aws")}, now_ms=NOW)
    assert (s.status, s.auth) == ("connected", "oauth")


def test_expired_bundle_is_needs_auth_with_the_expiry_time():
    exp = 1_759_688_640_000      # 2025-10-05 18:24 UTC
    creds = {"aws|1": _bundle("aws", token="secret-tok", expires=exp)}
    [s] = sanitize_statuses(_raw(("aws", "connected")), credentials=creds, now_ms=NOW)
    assert s.status == "needs-auth" and s.auth == "oauth"
    assert s.error == "Sign-in expired 2025-10-05 18:24 UTC — re-authenticate."
    assert "secret-tok" not in repr(s) and "secret-tok" not in str(s.to_wire())


def test_all_empty_bundles_are_not_signed_in():
    creds = {"aws|1": _bundle("aws", token=""), "aws|2": {"serverName": "aws"}}
    [s] = sanitize_statuses(_raw(("aws", "connected")), credentials=creds, now_ms=NOW)
    assert s.status == "needs-auth"
    assert s.error == "Not signed in — authenticate to use this server."


def test_cleared_bundle_with_zero_expiry_is_not_usable():
    creds = {"aws|1": _bundle("aws", token="", expires=0)}
    [s] = sanitize_statuses(_raw(("aws", "connected")), credentials=creds, now_ms=NOW)
    assert s.status == "needs-auth" and s.error.startswith("Not signed in")


def test_token_with_zero_expiry_is_cleared_not_usable():
    creds = {"aws|1": _bundle("aws", token="t", expires=0)}
    [s] = sanitize_statuses(_raw(("aws", "connected")), credentials=creds, now_ms=NOW)
    assert s.status == "needs-auth"


def test_disabled_server_is_left_alone():
    creds = {"aws|1": _bundle("aws", token="", expires=0)}
    [s] = sanitize_statuses(_raw(("aws", "disabled")), credentials=creds, now_ms=NOW)
    assert (s.status, s.error, s.auth) == ("disabled", None, None)


def test_one_usable_bundle_among_several_is_enough():
    creds = {"aws|old": _bundle("aws", token="", url="https://old/mcp"),
             "aws|1": _bundle("aws", expires=NOW - 1),
             "aws|2": _bundle("aws", expires=NOW + 1)}
    [s] = sanitize_statuses(_raw(("aws", "connected")), credentials=creds, now_ms=NOW)
    assert (s.status, s.auth) == ("connected", "oauth")


def test_garbage_credentials_are_ignored():
    [s] = sanitize_statuses(_raw(("aws", "connected")), credentials={"x": 1, "y": None}, now_ms=NOW)
    assert (s.status, s.auth) == ("connected", None)


def test_read_mcp_oauth_is_best_effort(tmp_path):
    import json
    from server.mcp_health.status import read_mcp_oauth
    assert read_mcp_oauth(tmp_path) == {}
    assert read_mcp_oauth(None) == {}
    (tmp_path / ".credentials.json").write_text("{not json")
    assert read_mcp_oauth(tmp_path) == {}
    (tmp_path / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"a": 1},
                                                            "mcpOAuth": {"aws|1": _bundle("aws")}}))
    assert read_mcp_oauth(str(tmp_path)) == {"aws|1": _bundle("aws")}
