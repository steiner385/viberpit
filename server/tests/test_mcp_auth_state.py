from server.mcp_auth import McpAuthState

def test_to_dict_camelcases_and_roundtrips():
    s = McpAuthState(state="awaiting_redirect", server="notion",
                     auth_url="https://x/y", accounts=["a", "b"], started_at=123.0)
    d = s.to_dict()
    assert d == {"state": "awaiting_redirect", "server": "notion", "message": None,
                 "authUrl": "https://x/y", "accounts": ["a", "b"], "startedAt": 123.0}
    assert McpAuthState.from_dict(d).server == "notion"

def test_defaults_are_idle():
    assert McpAuthState().to_dict()["state"] == "idle"
    assert McpAuthState().to_dict()["accounts"] == []
