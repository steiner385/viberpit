"""Unit tests for the SDLC per-stage seed-prompt builders — pure, no I/O.

Mirrors test_backlog_prompt: assert the builders emit the invariants each spawned
session must obey (commit path, non-draft PR, never edit upstream, co-authored-by).
"""
from __future__ import annotations

import pytest

from server.sdlc.prompts import (
    build_intent_prompt,
    build_plan_prompt,
    build_spec_prompt,
)

SLUG = "dark-mode"
INTENT_MD = "# Intent\n\nProblem: the UI is too bright.\n"
SPEC_MD = "# Spec\n\nAdd a theme toggle persisted per user.\n"
SKILLS = ["frontend-design", "eliminating-parallel-implementations"]


# --------------------------------------------------------------------------- #
# Shared invariants across all three builders                                  #
# --------------------------------------------------------------------------- #

def _all_prompts():
    return [
        build_intent_prompt("users keep asking for dark mode", slug=SLUG),
        build_spec_prompt(INTENT_MD, SKILLS, slug=SLUG),
        build_plan_prompt(SPEC_MD, slug=SLUG),
    ]


@pytest.mark.parametrize("prompt", _all_prompts())
def test_returns_nonempty_string(prompt):
    assert isinstance(prompt, str) and prompt.strip()


@pytest.mark.parametrize("prompt", _all_prompts())
def test_instructs_non_draft_pr(prompt):
    lower = prompt.lower()
    assert "non-draft" in lower or "not a draft" in lower or "never a draft" in lower


@pytest.mark.parametrize("prompt", _all_prompts())
def test_instructs_never_edit_upstream(prompt):
    lower = prompt.lower()
    assert "upstream" in lower and ("never" in lower or "do not" in lower or "don't" in lower)


@pytest.mark.parametrize("prompt", _all_prompts())
def test_mentions_slug_dir(prompt):
    assert f"docs/sdlc/{SLUG}/" in prompt


@pytest.mark.parametrize("prompt", _all_prompts())
def test_co_authored_by(prompt):
    assert "Co-Authored-By" in prompt


@pytest.mark.parametrize("prompt", _all_prompts())
def test_instructs_frontmatter_contract(prompt):
    """Issue #2161: every stage session must be told to write the initiative↔
    capability↔epic frontmatter fence, so the planning relationships are
    version-controlled instead of DB-only."""
    assert f"initiative: {SLUG}" in prompt
    assert "capabilities:" in prompt
    assert "epic:" in prompt
    assert "OPTIONAL" in prompt


def test_frontmatter_kind_matches_the_artifact_it_produces():
    assert "kind: intent" in build_intent_prompt("t", slug=SLUG)
    assert "kind: spec" in build_spec_prompt(INTENT_MD, SKILLS, slug=SLUG)
    assert "kind: plan" in build_plan_prompt(SPEC_MD, slug=SLUG)


# --------------------------------------------------------------------------- #
# intent                                                                       #
# --------------------------------------------------------------------------- #

def test_intent_targets_intent_md():
    p = build_intent_prompt("some trigger", slug=SLUG)
    assert f"docs/sdlc/{SLUG}/intent.md" in p


def test_intent_includes_trigger_text():
    p = build_intent_prompt("users keep asking for dark mode", slug=SLUG)
    assert "users keep asking for dark mode" in p


def test_intent_asks_for_required_sections():
    p = build_intent_prompt("t", slug=SLUG).lower()
    for section in ("problem", "outcome", "affected system", "constraint", "open question"):
        assert section in p


# --------------------------------------------------------------------------- #
# spec                                                                         #
# --------------------------------------------------------------------------- #

def test_spec_targets_spec_md():
    p = build_spec_prompt(INTENT_MD, SKILLS, slug=SLUG)
    assert f"docs/sdlc/{SLUG}/spec.md" in p


def test_spec_embeds_intent_and_skills():
    p = build_spec_prompt(INTENT_MD, SKILLS, slug=SLUG)
    assert "the UI is too bright" in p  # intent content carried in
    for skill in SKILLS:
        assert skill in p


def test_spec_flags_policy_concerns():
    p = build_spec_prompt(INTENT_MD, SKILLS, slug=SLUG).lower()
    assert "policy" in p and ("constraint" in p or "flag" in p)


# --------------------------------------------------------------------------- #
# spec — stable requirement IDs (issue #2163)                                  #
# --------------------------------------------------------------------------- #

def test_spec_asks_for_stable_requirement_ids():
    p = build_spec_prompt(INTENT_MD, SKILLS, slug=SLUG)
    assert "FR-NNN" in p
    assert "AC-NNN" in p
    assert "US-N" in p
    assert "## Requirements" in p


def test_spec_says_ids_are_permanent():
    p = build_spec_prompt(INTENT_MD, SKILLS, slug=SLUG).lower()
    assert "never renumber" in p or "never reuse" in p


def test_spec_with_no_skills_is_still_valid():
    p = build_spec_prompt(INTENT_MD, [], slug=SLUG)
    assert isinstance(p, str) and p.strip()
    assert f"docs/sdlc/{SLUG}/spec.md" in p


# --------------------------------------------------------------------------- #
# spec — persona docs injected from the linked capability (issue #2447)        #
# --------------------------------------------------------------------------- #

PERSONAS = [("parent", "# Parent persona\n\nRole: primary caregiver.\n")]


def test_spec_embeds_persona_docs():
    p = build_spec_prompt(INTENT_MD, SKILLS, slug=SLUG, personas=PERSONAS)
    assert "PERSONA `parent`" in p
    assert "# Parent persona" in p
    assert "Role: primary caregiver." in p


def test_spec_asks_stories_to_name_their_persona():
    p = build_spec_prompt(INTENT_MD, SKILLS, slug=SLUG, personas=PERSONAS).lower()
    assert "must name which persona" in p


def test_spec_with_no_personas_omits_the_persona_section():
    p = build_spec_prompt(INTENT_MD, SKILLS, slug=SLUG)
    assert "PERSONA `" not in p
    assert "must name which persona" not in p.lower()


def test_spec_with_multiple_personas_embeds_each_one():
    two = [("parent", "Parent doc body."), ("teen", "Teen doc body.")]
    p = build_spec_prompt(INTENT_MD, SKILLS, slug=SLUG, personas=two)
    assert "PERSONA `parent`" in p and "Parent doc body." in p
    assert "PERSONA `teen`" in p and "Teen doc body." in p


# --------------------------------------------------------------------------- #
# spec — constitution-aware fallback (issue #2165, item 3)                     #
# --------------------------------------------------------------------------- #

def test_spec_prompt_names_the_real_path_and_principles_when_ratified():
    text = build_spec_prompt(
        "intent body", [], slug="s", repo="o/r",
        constitution=(
            "# Constitution\n\n### I. First Principle\n\n### II. Second Principle\n\n"
            "**Version**: 1.0.0 | **Ratified**: 2026-01-01 | **Last Amended**: 2026-01-01"
        ),
        constitution_path=".specify/memory/constitution.md",
    )
    assert ".specify/memory/constitution.md" in text
    assert "I. First Principle" in text
    assert "II. Second Principle" in text
    assert "apply the repository constitution" not in text.lower() or "principles:" in text


def test_spec_prompt_flags_a_missing_constitution_instead_of_assuming_one():
    text = build_spec_prompt("intent body", [], slug="s", repo="o/r", constitution=None)
    assert "no constitution could be read" in text or "constitution" in text.lower()


def test_spec_prompt_flags_a_template_constitution():
    template = (
        "# [PROJECT_NAME] Constitution\n\n### [PRINCIPLE_1_NAME]\n\n"
        "**Version**: [CONSTITUTION_VERSION] | **Ratified**: [RATIFICATION_DATE] | "
        "**Last Amended**: [LAST_AMENDED_DATE]"
    )
    text = build_spec_prompt("intent body", [], slug="s", repo="o/r", constitution=template)
    assert "unfilled" in text.lower() or "template" in text.lower()
    assert "flag" in text.lower()


def test_spec_prompt_with_skills_ignores_constitution_entirely():
    """Org skills, when present, are still the binding constraint — the
    constitution block is only the fallback for when there are none."""
    text = build_spec_prompt(
        "intent body", ["skill-a"], slug="s", repo="o/r",
        constitution="# Constitution\n\n### I. P\n\n**Version**: 1.0.0 | **Ratified**: 2026-01-01 | **Last Amended**: 2026-01-01")
    assert "skill-a" in text
    assert "I. P" not in text


# --------------------------------------------------------------------------- #
# plan                                                                         #
# --------------------------------------------------------------------------- #

def test_plan_targets_plan_md():
    p = build_plan_prompt(SPEC_MD, slug=SLUG)
    assert f"docs/sdlc/{SLUG}/plan.md" in p


def test_plan_embeds_spec():
    p = build_plan_prompt(SPEC_MD, slug=SLUG)
    assert "theme toggle" in p


def test_plan_asks_for_files_order_tests_risks():
    p = build_plan_prompt(SPEC_MD, slug=SLUG).lower()
    for token in ("file", "order", "test", "risk"):
        assert token in p


def test_plan_tests_section_maps_tests_to_requirement_ids():
    p = build_plan_prompt(SPEC_MD, slug=SLUG)
    assert "FR-" in p and "AC-" in p and "US-" in p
    assert "proves" in p.lower()


# --------------------------------------------------------------------------- #
# default branch plumbing (mirrors backlog_prompt)                             #
# --------------------------------------------------------------------------- #

def test_default_branch_defaults_to_master():
    assert "origin/master" in build_intent_prompt("t", slug=SLUG)


def test_default_branch_can_be_main():
    p = build_plan_prompt(SPEC_MD, slug=SLUG, default_branch="main")
    assert "origin/main" in p
    assert "origin/master" not in p


# --------------------------------------------------------------------------- #
# artifact layout (#2416)                                                      #
# --------------------------------------------------------------------------- #

def test_spec_prompt_targets_the_resolved_layout():
    p = build_spec_prompt(INTENT_MD, SKILLS, slug=SLUG, layout="speckit")
    assert f"specs/{SLUG}/spec.md" in p
    assert "docs/sdlc" not in p


def test_plan_prompt_targets_the_resolved_layout():
    p = build_plan_prompt(SPEC_MD, slug=SLUG, layout="speckit")
    assert f"specs/{SLUG}/plan.md" in p
    assert "docs/sdlc" not in p


def test_unset_layout_still_defaults_to_docs_sdlc():
    p = build_plan_prompt(SPEC_MD, slug=SLUG)
    assert f"docs/sdlc/{SLUG}/plan.md" in p
