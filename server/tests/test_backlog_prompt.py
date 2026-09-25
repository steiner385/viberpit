"""Unit tests for backlog_prompt.build_implement_prompt / build_investigate_prompt —
pure, no I/O."""
from __future__ import annotations

import pytest

from dataclasses import replace

from server.automations.backlog_prompt import (
    CONVERTED_NOTE_PREFIX,
    DECISIONS_HEADING,
    build_implement_prompt,
    build_investigate_prompt,
)
from server.sessions.engine import AgentEngine
from server.automations.backlog_selection import Batch
from server.sessions.completion import NO_PR_MARKER, commit_trailer, resolution_from_text


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #

SINGLE_BATCH = Batch(
    primary="owner/repo#42",
    issues=["owner/repo#42"],
    repo="owner/repo",
)

MULTI_BATCH = Batch(
    primary="owner/repo#10",
    issues=["owner/repo#10", "owner/repo#20", "owner/repo#30"],
    repo="owner/repo",
)


# --------------------------------------------------------------------------- #
# Tests                                                                        #
# --------------------------------------------------------------------------- #

def test_returns_string():
    result = build_implement_prompt(SINGLE_BATCH)
    assert isinstance(result, str) and result


def test_single_issue_contains_fixes_keyword():
    result = build_implement_prompt(SINGLE_BATCH)
    assert "Fixes #42" in result


def test_instructs_citing_covers_ids_in_pr_body():
    """Issue #2163: the implement prompt tells the session to cite an issue's
    Covers: requirement IDs in the PR body."""
    result = build_implement_prompt(SINGLE_BATCH)
    assert "Covers:" in result
    assert "PR body" in result


def test_multi_issue_contains_fixes_for_each():
    result = build_implement_prompt(MULTI_BATCH)
    assert "Fixes #10" in result
    assert "Fixes #20" in result
    assert "Fixes #30" in result


def test_references_repo():
    result = build_implement_prompt(SINGLE_BATCH)
    assert "owner/repo" in result


def test_instructs_open_pr():
    result = build_implement_prompt(SINGLE_BATCH)
    lower = result.lower()
    assert "pull request" in lower or "pr" in lower


def test_instructs_mark_ready():
    result = build_implement_prompt(SINGLE_BATCH)
    lower = result.lower()
    assert "ready" in lower


def test_instructs_auto_merge():
    result = build_implement_prompt(SINGLE_BATCH)
    assert "auto-merge" in result or "--auto" in result


def test_instructs_one_pr_for_all_issues():
    """All issues go into one PR, not multiple."""
    result = build_implement_prompt(MULTI_BATCH)
    lower = result.lower()
    assert "one pull request" in lower or "single pull request" in lower or "one pr" in lower


def test_instructs_branch_off_default_branch_master():
    result = build_implement_prompt(SINGLE_BATCH, default_branch="master")
    assert "origin/master" in result


def test_instructs_branch_off_default_branch_main():
    result = build_implement_prompt(SINGLE_BATCH, default_branch="main")
    assert "origin/main" in result
    assert "origin/master" not in result


def test_default_branch_defaults_to_master():
    result = build_implement_prompt(SINGLE_BATCH)
    assert "origin/master" in result


def test_instructs_co_authored_by():
    result = build_implement_prompt(SINGLE_BATCH)
    assert "Co-Authored-By" in result


def test_each_issue_ref_mentioned():
    result = build_implement_prompt(MULTI_BATCH)
    for ref in MULTI_BATCH.issues:
        assert ref in result


@pytest.mark.parametrize("issues,expected_fixes", [
    (["a/b#1"], ["Fixes #1"]),
    (["x/y#99", "x/y#100"], ["Fixes #99", "Fixes #100"]),
])
def test_parametrized_fixes_keywords(issues, expected_fixes):
    batch = Batch(primary=issues[0], issues=issues, repo=issues[0].split("#")[0])
    result = build_implement_prompt(batch)
    for kw in expected_fixes:
        assert kw in result


# --------------------------------------------------------------------------- #
# Decisions already taken (spec 2026-09-18 §6)                                 #
# --------------------------------------------------------------------------- #

# The implement prompt exactly as it read BEFORE decisions were threaded through
# it, so the undecided path is pinned byte-for-byte: carrying a decision may add
# text to a DECIDED batch and must add nothing at all to an undecided one. The
# trailer is interpolated from the one ``commit_trailer`` the builder itself
# calls — it is engine-derived and is the only part of the prompt that is not a
# literal.
UNDECIDED_PROMPT = (
    "Implement the following GitHub issue(s) in repository `owner/repo` "
    "and land them in a single pull request:\n\n"
    "- owner/repo#42  (close with `Fixes #42` in the PR body)\n\n"
    "Requirements — follow the repository's completion workflow exactly:\n"
    "1. Implement all issues listed above.\n"
    "2. Open ONE pull request that covers all of them (never a draft).\n"
    "3. In the PR body include a closing keyword for EVERY issue:\n"
    "   `Fixes #<n>` (one per issue) so GitHub auto-closes them on merge.\n"
    "4. If an issue's body has a `Covers: FR-…, AC-…` line naming stable\n"
    "   requirement/acceptance-criteria IDs, cite those same IDs in the PR\n"
    "   body (e.g. `Covers: FR-003, AC-012`) so requirement coverage traces\n"
    "   from the PR back to the spec.\n"
    "5. Mark the PR ready for review immediately after opening.\n"
    "6. Arm auto-merge: `gh pr merge <#> --squash --auto`.\n"
    "   Both steps 5 and 6 are required — a ready PR with auto-merge armed\n"
    "   is the finished state.\n"
    "7. Branch off `origin/master` (never local `master`).\n"
    "8. End every commit message with:\n"
    f"   `{commit_trailer(AgentEngine.CLAUDE_CODE)}`\n"
)

# One accepted decision as ``server/pm/decisions.py:decision_text`` renders it.
# Quoted here the way the prompt must quote it: character for character.
DECISION_TEXT = (
    "Decision (Server-side): render it on the server — the client cannot see the "
    "ledger — because the mobile bundle must not grow.")


def test_undecided_prompt_is_byte_identical_to_the_pre_decision_prompt():
    """A batch with nothing decided (the dataclass default) must produce exactly
    the prompt the runner has always sent — threading decisions through the
    builder is additive or it is a silent rewrite of every dispatch."""
    assert build_implement_prompt(SINGLE_BATCH) == UNDECIDED_PROMPT
    assert DECISIONS_HEADING not in build_implement_prompt(SINGLE_BATCH)


def test_an_empty_decisions_map_adds_nothing():
    """Explicitly empty is the same as unset: no heading, no blank section, no
    stray newline — the same bytes."""
    assert build_implement_prompt(replace(SINGLE_BATCH, decisions={})) == UNDECIDED_PROMPT


def test_accepted_decision_is_quoted_verbatim():
    result = build_implement_prompt(
        replace(SINGLE_BATCH, decisions={"owner/repo#42": DECISION_TEXT}))
    assert DECISIONS_HEADING in result
    # Verbatim: the exact sentence the person accepted, prefixed only by its ref.
    assert f"- owner/repo#42: {DECISION_TEXT}" in result


def test_the_decision_block_is_purely_additive():
    """Everything OUTSIDE the block is byte-identical to the undecided prompt, so
    a decision adds an instruction and changes no other one."""
    result = build_implement_prompt(
        replace(SINGLE_BATCH, decisions={"owner/repo#42": DECISION_TEXT}))
    start = result.index(DECISIONS_HEADING)
    end = result.index("Requirements — follow")
    assert result[:start] + result[end:] == UNDECIDED_PROMPT


def test_decisions_follow_the_batch_issue_order_and_skip_undecided_issues():
    result = build_implement_prompt(replace(
        MULTI_BATCH, decisions={"owner/repo#30": "Decision (C): third.",
                                "owner/repo#10": "Decision (A): first."}))
    block = result[result.index(DECISIONS_HEADING):result.index("Requirements — follow")]
    assert block.index("Decision (A): first.") < block.index("Decision (C): third.")
    assert "owner/repo#20" not in block          # nothing decided for that issue


def test_a_decision_for_a_ref_outside_the_batch_still_renders():
    """The batch's issues can be trimmed between the decision and the dispatch;
    dropping the decision silently would send the run off to re-litigate it."""
    result = build_implement_prompt(
        replace(SINGLE_BATCH, decisions={"owner/repo#99": "Decision (B): keep it."}))
    assert "- owner/repo#99: Decision (B): keep it." in result


def test_the_investigate_prompt_carries_no_decisions():
    """Only the IMPLEMENT path takes decisions: an investigation is asking what
    the problem is, and its four exits are its whole contract."""
    result = build_investigate_prompt(
        replace(SINGLE_BATCH, decisions={"owner/repo#42": DECISION_TEXT}),
        engine=AgentEngine.CLAUDE_CODE)
    assert DECISIONS_HEADING not in result
    assert DECISION_TEXT not in result


# --------------------------------------------------------------------------- #
# build_investigate_prompt                                                     #
# --------------------------------------------------------------------------- #

def test_investigate_returns_string():
    result = build_investigate_prompt(SINGLE_BATCH, engine=AgentEngine.CLAUDE_CODE)
    assert isinstance(result, str) and result


def test_investigate_references_repo():
    result = build_investigate_prompt(SINGLE_BATCH, engine=AgentEngine.CLAUDE_CODE)
    assert "owner/repo" in result


def test_investigate_each_issue_ref_mentioned():
    result = build_investigate_prompt(MULTI_BATCH, engine=AgentEngine.CLAUDE_CODE)
    for ref in MULTI_BATCH.issues:
        assert ref in result


def test_investigate_names_primary_ref():
    result = build_investigate_prompt(MULTI_BATCH, engine=AgentEngine.CLAUDE_CODE)
    assert MULTI_BATCH.primary in result


def test_investigate_names_all_four_exits():
    result = build_investigate_prompt(SINGLE_BATCH, engine=AgentEngine.CLAUDE_CODE)
    assert "Close as noise" in result
    assert "Consolidate" in result
    assert "Convert to a fix" in result
    assert "Escalate" in result


def test_investigate_mentions_mark_no_pr_but_not_set_hold_on_claude():
    """Exit 3 must not tell the session to call set_hold — it's a SIGNAL-class
    tool Codex never has (durable_tool_surface=False), and the ruling made exit 3
    engine-neutral: the runner clears the hold itself once it sees the
    'converted to a fix' note (Task 4), not the session."""
    result = build_investigate_prompt(SINGLE_BATCH, engine=AgentEngine.CLAUDE_CODE)
    assert "mark_no_pr" in result
    assert "set_hold" not in result
    assert CONVERTED_NOTE_PREFIX in result


def test_investigate_says_read_only_no_pr():
    """The exact sentence, not a disjunction whose second arm is a substring of the
    first: "no pr" already covers "open no pr", so the alternative could never
    change the outcome and only read as a second check."""
    result = build_investigate_prompt(SINGLE_BATCH, engine=AgentEngine.CLAUDE_CODE)
    assert "Read-only on code: open no PR, push nothing." in result


def test_investigate_frames_investigate_not_implement():
    result = build_investigate_prompt(SINGLE_BATCH, engine=AgentEngine.CLAUDE_CODE)
    assert "do not implement" in result.lower()


def test_investigate_states_no_exit_hand_off():
    result = build_investigate_prompt(SINGLE_BATCH, engine=AgentEngine.CLAUDE_CODE)
    assert "hands the issue to the owner" in result


def test_investigate_omits_implement_prompt_pr_instructions():
    """The investigate prompt must not carry the implement prompt's PR mechanics."""
    implement = build_implement_prompt(SINGLE_BATCH)
    investigate = build_investigate_prompt(SINGLE_BATCH, engine=AgentEngine.CLAUDE_CODE)
    # Phrases only the implement prompt should carry.
    assert "single pull request" in implement
    assert "single pull request" not in investigate
    assert "pull request" not in investigate.lower()
    assert "gh pr merge" in implement
    assert "gh pr merge" not in investigate
    assert "Fixes #" in implement
    assert "Fixes #" not in investigate


def test_investigate_default_engine_is_claude():
    default_result = build_investigate_prompt(SINGLE_BATCH)
    claude_result = build_investigate_prompt(SINGLE_BATCH, engine=AgentEngine.CLAUDE_CODE)
    assert default_result == claude_result


def test_investigate_codex_uses_no_pr_marker_not_the_tool():
    result = build_investigate_prompt(SINGLE_BATCH, engine=AgentEngine.CODEX)
    assert NO_PR_MARKER in result
    assert "mark_no_pr(" not in result


def test_investigate_codex_still_names_all_four_exits():
    result = build_investigate_prompt(SINGLE_BATCH, engine=AgentEngine.CODEX)
    assert "Close as noise" in result
    assert "Consolidate" in result
    assert "Convert to a fix" in result
    assert "Escalate" in result


def test_investigate_codex_escalate_reads_as_cancelled():
    """The escalate exit's Codex phrasing must classify as 'cancelled' via the
    same resolution_from_text the runner uses on a real transcript tail."""
    result = build_investigate_prompt(SINGLE_BATCH, engine=AgentEngine.CODEX)
    # Extract a plausible final-message tail: the escalate sentence plus the marker.
    idx = result.index("Escalate")
    tail = result[idx:]
    assert resolution_from_text(tail) == "cancelled"


def test_investigate_codex_close_as_noise_reads_as_not_needed():
    result = build_investigate_prompt(SINGLE_BATCH, engine=AgentEngine.CODEX)
    idx = result.index("Close as noise")
    tail = result[idx:result.index("Consolidate")]
    assert resolution_from_text(tail) == "not-needed"
