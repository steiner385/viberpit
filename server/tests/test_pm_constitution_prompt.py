"""Pure constitution-proposal prompt builder (issue #2259, item 2 of #2165).

Mirrors ``test_pm_classification.py``'s map-synthesis prompt tests: the same
propose-only guarantees (one draft PR, auto-merge never armed, a commit
trailer from the single source of truth) apply here, plus the sources this
prompt must point the agent at (ADRs, AGENTS.md, spec-kit templates) and the
governance-document path it must write."""
from __future__ import annotations

import pytest

from server.pm.classification import (
    build_constitution_proposal_prompt,
    constitution_branch_name,
)
from server.sessions.completion import commit_trailer
from server.sessions.engine import AgentEngine


def test_branch_name_is_deterministic_per_project():
    assert constitution_branch_name("viberpit") == "pm/constitution-viberpit"
    assert constitution_branch_name("viberpit") == constitution_branch_name("viberpit")
    assert constitution_branch_name("viberpit") != constitution_branch_name("other")


def test_prompt_is_propose_only_one_pr_never_auto_merged():
    p = build_constitution_proposal_prompt(
        slug="viberpit", repo="steiner385/viberpit", default_branch="master")
    assert "ONE" in p and "draft" in p.lower() and "non-draft" not in p.lower()
    assert "do not arm auto-merge" in p.lower()
    assert commit_trailer(AgentEngine.CLAUDE_CODE) in p
    assert "origin/master" in p
    assert "pm/constitution-viberpit" in p


def test_prompt_names_the_governance_document_path():
    p = build_constitution_proposal_prompt(
        slug="viberpit", repo="steiner385/viberpit", default_branch="master")
    assert ".specify/memory/constitution.md" in p


def test_prompt_points_at_adrs_agents_md_and_spec_kit_templates():
    p = build_constitution_proposal_prompt(
        slug="viberpit", repo="steiner385/viberpit", default_branch="master")
    assert "ADR" in p
    assert "AGENTS.md" in p
    assert "spec-kit" in p.lower()


def test_prompt_names_the_version_footer_grammar():
    p = build_constitution_proposal_prompt(
        slug="viberpit", repo="steiner385/viberpit", default_branch="master")
    assert "**Version**" in p and "**Ratified**" in p and "**Last Amended**" in p


@pytest.mark.parametrize("status,expected_fragment", [
    ("missing", "no governance document at all"),
    ("template", "unfilled spec-kit template"),
    ("no_version_footer", "Version/Ratified/Last Amended footer"),
    (None, "needs review"),
])
def test_prompt_opening_reflects_the_governance_gap(status, expected_fragment):
    p = build_constitution_proposal_prompt(
        slug="viberpit", repo="steiner385/viberpit", default_branch="master",
        constitution_status=status)
    assert expected_fragment in p


def test_prompt_carries_the_codex_commit_trailer_when_engine_is_codex():
    p = build_constitution_proposal_prompt(
        slug="viberpit", repo="steiner385/viberpit", default_branch="master",
        engine=AgentEngine.CODEX)
    assert commit_trailer(AgentEngine.CODEX) in p
    assert commit_trailer(AgentEngine.CLAUDE_CODE) not in p
