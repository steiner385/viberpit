from server.sessions.completion import COMPLETION_MARKER, COMPLETION_INSTRUCTION


def test_marker_is_html_comment():
    assert COMPLETION_MARKER == "<!-- ccswitch:complete -->"


def test_instruction_embeds_the_marker_verbatim():
    # The injected instruction and the detector must reference the same literal.
    assert COMPLETION_MARKER in COMPLETION_INSTRUCTION
    # It should tell the agent to emit it only when genuinely done.
    assert "only" in COMPLETION_INSTRUCTION.lower()
