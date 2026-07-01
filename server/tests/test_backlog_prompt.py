"""Unit tests for backlog_prompt.build_implement_prompt — pure, no I/O."""
from __future__ import annotations

import pytest

from server.automations.backlog_prompt import build_implement_prompt
from server.automations.backlog_selection import Batch


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


def test_instructs_branch_off_origin_master():
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
