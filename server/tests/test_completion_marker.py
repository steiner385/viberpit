from server.sessions.completion import NO_PR_MARKER, NO_PR_INSTRUCTION, resolution_from_text


def test_marker_absent_returns_none():
    assert resolution_from_text("all done, PR opened") is None


def test_marker_defaults_to_not_needed():
    assert resolution_from_text(f"Nothing to ship.\n{NO_PR_MARKER}") == "not-needed"


def test_marker_with_cancel_phrasing_returns_cancelled():
    txt = f"User asked to cancel this PR work, dropping it.\n{NO_PR_MARKER}"
    assert resolution_from_text(txt) == "cancelled"


def test_no_pr_marker_is_html_comment():
    assert NO_PR_MARKER == "<!-- ccswitch:no-pr -->"


def test_no_pr_instruction_embeds_marker():
    assert NO_PR_MARKER in NO_PR_INSTRUCTION


def test_sdk_factory_append_contains_no_pr_marker():
    # sdk_factory appends both COMPLETION_INSTRUCTION and NO_PR_INSTRUCTION
    from server.sessions.sdk_factory import _build_append_string
    append = _build_append_string()
    assert NO_PR_MARKER in append
