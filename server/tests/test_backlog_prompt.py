"""Unit tests for backlog_prompt.build_implement_prompt / build_investigate_prompt —
pure, no I/O."""
from __future__ import annotations

import pytest

from server.automations.backlog_prompt import (
    build_implement_prompt,
    build_investigate_prompt,
)
from server.sessions.engine import AgentEngine
from server.automations.backlog_selection import Batch
from server.sessions.completion import NO_PR_MARKER, resolution_from_text


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


def test_investigate_mentions_mark_no_pr_and_set_hold_on_claude():
    result = build_investigate_prompt(SINGLE_BATCH, engine=AgentEngine.CLAUDE_CODE)
    assert "mark_no_pr" in result
    assert "set_hold" in result


def test_investigate_says_read_only_no_pr():
    result = build_investigate_prompt(SINGLE_BATCH, engine=AgentEngine.CLAUDE_CODE)
    lower = result.lower()
    assert "read-only" in lower
    assert "no pr" in lower or "open no pr" in lower


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
