from server.sessions.completion import NO_PR_MARKER, NO_PR_INSTRUCTION, resolution_from_text


def test_marker_absent_returns_none():
    assert resolution_from_text("all done, PR opened") is None


def test_marker_defaults_to_not_needed():
    assert resolution_from_text(f"Nothing to ship.\n{NO_PR_MARKER}") == "not-needed"


def test_marker_with_cancel_phrasing_returns_cancelled():
    txt = f"User asked to cancel this PR work, dropping it.\n{NO_PR_MARKER}"
    assert resolution_from_text(txt) == "cancelled"


def test_no_pr_marker_is_html_comment():
    assert NO_PR_MARKER == "<!-- viberpit:no-pr -->"


def test_no_pr_instruction_embeds_marker():
    assert NO_PR_MARKER in NO_PR_INSTRUCTION


def test_sdk_factory_append_contains_no_pr_marker():
    """With the session-tools surface off, sdk_factory falls back to the marker
    contract, appending both COMPLETION_INSTRUCTION and NO_PR_INSTRUCTION. With
    tools mounted (the default), `mark_no_pr` replaces the marker instead — never
    both (task-8, marker-to-tool migration)."""
    from server.sessions.claude_sdk_factory import _build_append_string
    append = _build_append_string()
    assert "mark_no_pr" in append
    assert NO_PR_MARKER not in append


def test_no_pr_instruction_excludes_unfinished_code_objectives():
    """A no-pr marker is NOT the way to hand back an implementation objective that
    turned out to need a spec / decision / more work (prod 2026-09-06, fdc6b27a):
    the instruction must say so and route that case to a question with no marker."""
    from server.sessions.completion import NO_PR_INSTRUCTION
    low = NO_PR_INSTRUCTION.lower()
    assert "already satisfied" in low
    assert "not a reason" in low
    assert "no marker" in low
