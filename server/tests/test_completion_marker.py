from server.sessions.completion import NO_PR_MARKER, NO_PR_INSTRUCTION, resolution_from_text


def test_marker_absent_returns_none():
    assert resolution_from_text("all done, PR opened") is None


def test_marker_defaults_to_not_needed():
    assert resolution_from_text(f"Nothing to ship.\n{NO_PR_MARKER}") == "not-needed"


def test_marker_with_cancel_phrasing_returns_cancelled():
    txt = f"User asked to cancel this PR work, dropping it.\n{NO_PR_MARKER}"
    assert resolution_from_text(txt) == "cancelled"


def test_the_declared_token_returns_the_decision_park():
    """The MARKER half of spec 2026-09-18 §6. An engine without the durable tool
    surface (Codex) proposes through the decisions route and then ends its turn with
    the no-PR marker, so ``NO_PR_INSTRUCTION`` asks it to write the exact token —
    without which the ending would read as ``not-needed``, settling the dispatch as
    "nothing to do" while the issue sits on an unanswered question."""
    from server.sessions.completion import AWAITING_DECISION
    txt = f"Proposed two options — awaiting-decision on #12.\n{NO_PR_MARKER}"
    assert resolution_from_text(txt) == AWAITING_DECISION
    assert AWAITING_DECISION in NO_PR_INSTRUCTION, "the instruction must name the token"


def test_prose_about_awaiting_a_decision_is_not_the_token():
    """Review R1 F2: only the DECLARED token counts. "awaiting a decision from you"
    is ordinary English in an ending that is really a cancellation or a hand-back,
    and a pattern that matched it re-labelled those endings silently — the
    ``no_pr_resolution_disagreement`` log never fires on a pure-marker turn."""
    txt = f"Nothing to ship here; awaiting a decision from you on the naming.\n{NO_PR_MARKER}"
    assert resolution_from_text(txt) == "not-needed"


def test_a_cancelled_ending_that_mentions_a_decision_still_reads_cancelled():
    """The cancellation heuristic keeps its precedence over prose: only the exact
    token outranks it."""
    txt = ("Dropping this work — awaiting a decision from the owner before anyone "
           f"retries.\n{NO_PR_MARKER}")
    assert resolution_from_text(txt) == "cancelled"


def test_the_token_outranks_the_cancellation_heuristic():
    """A declaration beats a guess, the same precedence the Translator applies when a
    tool call and a marker disagree — and such a turn very often also says it is
    "not opening a PR", which ``_CANCEL_RE`` matches."""
    txt = ("Not opening a PR yet: the choice between the two behaviours is yours. "
           f"awaiting-decision\n{NO_PR_MARKER}")
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
