from server.sessions.completion import NO_PR_MARKER, NO_PR_INSTRUCTION, resolution_from_text


def test_marker_absent_returns_none():
    assert resolution_from_text("all done, PR opened") is None


def test_marker_defaults_to_not_needed():
    assert resolution_from_text(f"Nothing to ship.\n{NO_PR_MARKER}") == "not-needed"


def test_marker_with_cancel_phrasing_returns_cancelled():
    txt = f"User asked to cancel this PR work, dropping it.\n{NO_PR_MARKER}"
    assert resolution_from_text(txt) == "cancelled"


def test_marker_naming_an_awaiting_decision_returns_that_resolution():
    """The MARKER half of spec 2026-09-18 §6. An engine without the durable tool
    surface (Codex) proposes through the decisions route and then ends its turn with
    the no-PR marker, so the only place the park can be legible is the prose — and
    it must not read as ``not-needed``, which would settle the dispatch as "nothing
    to do" while the issue sits on an unanswered question."""
    from server.sessions.completion import AWAITING_DECISION
    txt = f"Proposed two options; awaiting a decision before I build.\n{NO_PR_MARKER}"
    assert resolution_from_text(txt) == AWAITING_DECISION
    assert resolution_from_text(f"Awaiting-decision on #12.\n{NO_PR_MARKER}") == AWAITING_DECISION


def test_an_awaiting_decision_outranks_the_cancellation_heuristic():
    """Such a turn almost always also says it is not opening a PR — which
    ``_CANCEL_RE`` matches. The decision park is the more specific claim and the one
    the runner acts on, so it is tested first."""
    txt = ("Not opening a PR yet: the choice between the two behaviours is yours. "
           f"Awaiting decision.\n{NO_PR_MARKER}")
    assert resolution_from_text(txt) == "awaiting-decision"


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
    the instruction must say so and route that case to the hand-back marker."""
    from server.sessions.completion import NO_PR_INSTRUCTION
    low = NO_PR_INSTRUCTION.lower()
    assert "already satisfied" in low
    assert "not a reason" in low
    assert "hand-back" in low
