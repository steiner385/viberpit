"""Pure classification module tests: prompt builders + structured-output parse
(design 2026-08-31 §5). Phase 3 reuses this module, so the schema is pinned."""
from __future__ import annotations

import pytest

from server.pm.classification import (
    CLASSIFY_CHUNK_SIZE,
    ClassificationParseError,
    FeatureBrief,
    ProposedClassification,
    WorkItemBrief,
    build_classification_prompt,
    build_map_synthesis_prompt,
    feature_doc_path,
    parse_classification_output,
)


def _item(n: int) -> WorkItemBrief:
    return WorkItemBrief(number=n, kind="issue", title=f"Item {n}",
                         body="Something is broken", labels=("bug",),
                         url=f"https://github.com/o/r/issues/{n}")


def _feature(slug: str) -> FeatureBrief:
    return FeatureBrief(slug=slug, title=slug.title(), summary="A capability.")


def test_feature_doc_path_is_single_source():
    assert feature_doc_path("sessions") == "docs/product/features/sessions.md"


def test_chunk_size_matches_spec():
    assert CLASSIFY_CHUNK_SIZE == 50


def test_map_prompt_is_propose_only_one_pr():
    """Existing propose-only guarantees must still hold after the census rework."""
    p = build_map_synthesis_prompt(
        slug="ccswitch", repo="steiner385/ccswitch", default_branch="master",
        open_count=120, closed_count=800)
    assert "docs/product/features/" in p
    assert "ONE" in p and "non-draft" in p.lower()
    assert "auto-merge" in p.lower()          # explicitly forbidden
    assert "Evidence" in p                    # outcome-driven template marker
    assert "120" in p and "800" in p          # backlog scale is context
    assert "Co-Authored-By: Claude Opus 4.8" in p  # commit trailer
    assert "origin/" in p                    # origin/ branch instruction


def test_map_prompt_reads_authoritative_docs_first():
    """Step A: prompt must instruct reading README/CLAUDE.md/docs before code."""
    p = build_map_synthesis_prompt(
        slug="ccswitch", repo="steiner385/ccswitch", default_branch="master",
        open_count=50, closed_count=200)
    # Authoritative docs step must be named and come before census
    assert "Step A" in p
    assert "README" in p
    assert "CLAUDE.md" in p or "AGENTS.md" in p
    step_a_pos = p.index("Step A")
    step_b_pos = p.index("Step B")
    assert step_a_pos < step_b_pos, "Step A (read docs first) must precede Step B (census)"


def test_map_prompt_has_census_step_with_entry_points_routers_models():
    """Step B: census must enumerate CLI entry points, routers, and DB models."""
    p = build_map_synthesis_prompt(
        slug="ccswitch", repo="steiner385/ccswitch", default_branch="master",
        open_count=50, closed_count=200)
    assert "Step B" in p
    # Entry points / CLI
    assert "entry point" in p.lower() or "console_scripts" in p or "argparse" in p
    # Web / API routers
    assert "router" in p.lower()
    # DB models/tables
    assert "model" in p.lower() or "table" in p.lower()
    # Config surface
    assert "config" in p.lower() or "env var" in p.lower()


def test_map_prompt_anti_churn_bias_rule_is_explicit():
    """The prompt must state explicitly that churn signals activity not importance."""
    p = build_map_synthesis_prompt(
        slug="ccswitch", repo="steiner385/ccswitch", default_branch="master",
        open_count=50, closed_count=200)
    # Anti-bias rule must be stated
    assert "churn" in p.lower()
    assert "activity" in p.lower() and "importance" in p.lower()
    # Must call out stable/foundational code as important despite low churn
    assert "stable" in p.lower() or "foundational" in p.lower() or "fundamental" in p.lower()


def test_map_prompt_coverage_audit_in_pr_body():
    """Step E: coverage table must be required and placed in the PR body."""
    p = build_map_synthesis_prompt(
        slug="ccswitch", repo="steiner385/ccswitch", default_branch="master",
        open_count=50, closed_count=200)
    assert "Step E" in p
    assert "coverage" in p.lower()
    # Must instruct session to put coverage table in PR body
    assert "PR body" in p or "pull request body" in p.lower()
    # Step D must come before Step E (coverage audit comes after writing docs)
    # Use the section header format to find the actual step sections
    step_d_pos = p.index("## Step D")
    step_e_pos = p.index("## Step E")
    assert step_d_pos < step_e_pos, "Step D (write docs) must precede Step E (coverage audit)"


def test_map_prompt_churn_is_secondary_not_discovery():
    """Step C: churn enrichment must be labelled as SECONDARY — not discovery."""
    p = build_map_synthesis_prompt(
        slug="ccswitch", repo="steiner385/ccswitch", default_branch="master",
        open_count=50, closed_count=200)
    assert "Step C" in p
    step_b_pos = p.index("Step B")
    step_c_pos = p.index("Step C")
    assert step_b_pos < step_c_pos, "Census (Step B) must precede churn enrichment (Step C)"
    # The word SECONDARY must appear in Step C context
    assert "SECONDARY" in p or "secondary" in p.lower()


def test_classification_prompt_lists_items_and_features():
    p = build_classification_prompt(
        items=[_item(7), _item(8)],
        features=[_feature("sessions"), _feature("triage")])
    assert "#7" in p and "#8" in p
    assert "sessions" in p and "triage" in p
    assert "no_fit" in p                        # the escape hatch is offered
    assert "JSON" in p                          # structured output requested


def test_classification_prompt_injects_corrections():
    p = build_classification_prompt(
        items=[_item(1)], features=[_feature("a")],
        corrections=["#4 was NOT sessions; it is billing"])
    assert "billing" in p


def test_parse_valid_output():
    text = """Here is my classification:
    ```json
    [
      {"number": 7, "kind": "issue", "feature_slug": "sessions",
       "reach": 3, "impact": 2, "effort": 1, "confidence": 2,
       "rationale": "touches the session manager", "confidence_level": "high"},
      {"number": 8, "kind": "issue", "feature_slug": null,
       "no_fit_suggestion": "billing", "rationale": "no matching feature",
       "confidence_level": "low"}
    ]
    ```
    """
    out = parse_classification_output(text)
    assert out[0] == ProposedClassification(
        number=7, kind="issue", feature_slug="sessions", no_fit_suggestion=None,
        reach=3, impact=2, effort=1, confidence=2,
        rationale="touches the session manager", confidence_level="high")
    assert out[1].feature_slug is None
    assert out[1].no_fit_suggestion == "billing"
    assert out[1].confidence_level == "low"


def test_parse_skips_malformed_rows_but_keeps_valid():
    text = '[{"number": 1, "kind": "issue", "feature_slug": "a",' \
           ' "rationale": "ok", "confidence_level": "medium"},' \
           ' {"no_number_here": true}]'
    out = parse_classification_output(text)
    assert [c.number for c in out] == [1]


def test_parse_no_json_raises():
    with pytest.raises(ClassificationParseError):
        parse_classification_output("I could not classify anything, sorry.")


def test_parse_defaults_confidence_level_and_scores():
    text = '[{"number": 5, "kind": "pr", "feature_slug": "a", "rationale": "x"}]'
    (c,) = parse_classification_output(text)
    assert c.confidence_level == "low"   # missing → conservative default
    assert c.reach is None and c.impact is None
