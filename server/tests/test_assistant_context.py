from __future__ import annotations

from server.assistant.context import render_context_header, validate_context
from server.sessions.events import SessionMeta, UserMessage


def test_validate_context():
    assert validate_context({"route": "/sessions/abc-123", "sessionId": "abc-123-def-456"}) == {
        "route": "/sessions/abc-123", "sessionId": "abc-123-def-456"}
    assert validate_context({"route": "/etc/passwd"}) is None
    assert validate_context({"route": "/sessions", "sessionId": "<x>"}) is None
    assert validate_context("nope") is None
    # `$` (not `\Z`) matches just before a single trailing "\n" — a smuggled
    # newline must not slip through and reach the wire unescaped.
    assert validate_context({"route": "/sessions\n"}) is None
    assert validate_context({"route": "/sessions", "sessionId": "abcdefgh\n"}) is None


def test_header_is_single_line_and_escaped():
    h = render_context_header({"route": "/pipeline", "sessionId": "abc"}, host="dobby")
    assert h == '<ccswitch-context route="/pipeline" session="abc" host="dobby" />'
    assert "\n" not in h


def test_user_message_carries_context_on_wire():
    ev = UserMessage(turn_id="t", text="hi", context={"route": "/sessions"})
    assert ev.to_wire()["context"] == {"route": "/sessions"}
    assert UserMessage(turn_id="t", text="hi").to_wire()["context"] is None
    meta = SessionMeta(model="sonnet", session_id="s1", kind="assistant")
    assert meta.to_wire()["kind"] == "assistant"
    assert SessionMeta(model="sonnet", session_id="s1").to_wire()["kind"] is None
