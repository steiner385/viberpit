"""Classification of live claude processes into sdk/helper/session/other."""
from __future__ import annotations

from server.sessions.adopt import classify


def test_classify_sdk_owned_driver():
    cmd = [
        "/x/claude_agent_sdk/_bundled/claude", "--output-format", "stream-json",
        "--resume", "abc", "--input-format", "stream-json",
    ]
    assert classify(cmd) == "sdk"


def test_classify_helper_processes():
    assert classify(["/x/claude/versions/2.1", "--bg-pty-host", "/tmp/x.sock"]) == "helper"
    assert classify(["/x/claude/versions/2.1", "--bg-spare", "/tmp/x.sock"]) == "helper"
    assert classify(["/x/claude/versions/2.1", "daemon", "run", "--origin", "transient"]) == "helper"
    assert classify(["claude", "auth", "login", "--claudeai"]) == "helper"


def test_classify_interactive_session_is_adoptable():
    assert classify(["claude", "agents"]) == "session"
    assert classify(["claude"]) == "session"


def test_classify_non_claude_is_other():
    assert classify(["node", "/x/storybook/bin.js"]) == "other"
    assert classify([]) == "other"
