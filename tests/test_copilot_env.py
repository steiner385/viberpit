from ccswitch import copilot


def test_launch_env_points_at_loopback_proxy():
    env = copilot.launch_env(4142, "s3cret")
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:4142"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "s3cret"
    # non-first-party base URL disables MCP tool search unless re-enabled
    assert env["ENABLE_TOOL_SEARCH"] == "true"
