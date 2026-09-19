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
    map_branch_name,
    parse_classification_output,
)
from server.pm.fenced_json import FencedJsonError, extract_fenced_json
from server.sessions.completion import commit_trailer

from ccswitch.engines import AgentEngine


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
        slug="viberpit", repo="steiner385/viberpit", default_branch="master",
        open_count=120, closed_count=800)
    assert "docs/product/features/" in p
    assert "ONE" in p and "non-draft" in p.lower()
    assert "auto-merge" in p.lower()          # explicitly forbidden
    assert "Evidence" in p                    # outcome-driven template marker
    assert "120" in p and "800" in p          # backlog scale is context
    # Commit trailer, interpolated from the single source of truth rather than
    # pinned here — a second copy of the literal is how the stale "Opus 4.8"
    # version survived a model bump in the first place.
    assert commit_trailer(AgentEngine.CLAUDE_CODE) in p
    assert "origin/" in p                    # origin/ branch instruction


def test_map_prompt_reads_authoritative_docs_first():
    """Step A: prompt must instruct reading README/CLAUDE.md/docs before code."""
    p = build_map_synthesis_prompt(
        slug="viberpit", repo="steiner385/viberpit", default_branch="master",
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
        slug="viberpit", repo="steiner385/viberpit", default_branch="master",
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
        slug="viberpit", repo="steiner385/viberpit", default_branch="master",
        open_count=50, closed_count=200)
    # Anti-bias rule must be stated
    assert "churn" in p.lower()
    assert "activity" in p.lower() and "importance" in p.lower()
    # Must call out stable/foundational code as important despite low churn
    assert "stable" in p.lower() or "foundational" in p.lower() or "fundamental" in p.lower()


def test_map_prompt_coverage_audit_in_pr_body():
    """Step E: coverage table must be required and placed in the PR body."""
    p = build_map_synthesis_prompt(
        slug="viberpit", repo="steiner385/viberpit", default_branch="master",
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
        slug="viberpit", repo="steiner385/viberpit", default_branch="master",
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


def test_initial_map_prompt_pins_deterministic_branch():
    p = build_map_synthesis_prompt(
        slug="acme", repo="o/acme", default_branch="main",
        open_count=5, closed_count=9, scope="initial")
    assert map_branch_name("acme") == "pm/feature-map-acme"
    assert "pm/feature-map-acme" in p          # the branch is named in the prompt


def test_coverage_critic_prompt_is_same_branch_and_census_driven():
    from server.pm.classification import build_coverage_critic_prompt
    p = build_coverage_critic_prompt(
        slug="acme", repo="o/acme", default_branch="main",
        branch="pm/feature-map-acme", open_count=5, closed_count=9)
    pl = p.lower()
    # Independent second-agent census (the anti-omission check)
    assert "census" in pl
    assert any(k in pl for k in ["cli", "console", "entry point"])
    assert any(k in pl for k in ["router", "endpoint"])
    assert "model" in pl
    # Works on the SAME open PR branch — must NOT open a new PR or arm auto-merge
    assert "pm/feature-map-acme" in p
    assert "do not open a new pull request" in pl or "do not open a new pr" in pl
    assert "auto-merge" in pl  # instructed never to arm it
    # Anti-bias: low-churn foundations are the target, not skip-worthy
    assert any(k in pl for k in ["churn", "activity, not importance", "fundamental"])


def test_diff_prompt_arms_auto_merge_with_hold_human_takeover():
    """Feature-map drift PRs are FULLY AUTOMATED by default (2026-09-08).

    The old `scope="diff"` prompt said "Do NOT arm auto-merge — the merge is the
    human curation gate". That gate never existed in the target repo: KinDash's
    `main` requires zero reviews and its ops-auto-merge workflow arms every open
    non-draft PR anyway, so agents dutifully left auto-merge off and the bot
    merged the PR unreviewed minutes later (#15829 merged at 20:08:41Z with 0
    reviews after the session deliberately did not arm it). The instruction
    bought nothing and made agents write a false claim into PR bodies.

    Human takeover is now the exception, expressed as a `hold:human` label the
    agent applies when its own confidence is low.
    """
    p = build_map_synthesis_prompt(
        slug="acme", repo="acme/app", default_branch="main",
        open_count=10, closed_count=20, scope="diff",
        suggestion="new-capability", item_numbers=(1, 2))
    pl = p.lower()
    assert "arm auto-merge" in pl
    assert "do not arm auto-merge" not in pl
    assert "human curation gate" not in pl
    # The takeover lever, and when to reach for it.
    assert "hold:human" in p
    assert "confidence" in pl


def test_diff_prompt_keeps_its_other_propose_guarantees():
    """Arming auto-merge must not loosen the rest of the contract."""
    p = build_map_synthesis_prompt(
        slug="acme", repo="acme/app", default_branch="main",
        open_count=10, closed_count=20, scope="diff",
        suggestion="new-capability", item_numbers=(1, 2))
    assert "docs/product/features/" in p
    assert "ONE" in p and "non-draft" in p.lower()
    assert "origin/main" in p
    assert "Co-Authored-By" in p
    # ...but the surviving guarantee is SCOPE, not a merge gate. "propose-only"
    # is a term of art here (the SDLC chain uses it for "a human merges"), so
    # leaving it on a prompt that arms auto-merge re-states the false claim this
    # change exists to delete.
    assert "propose-only" not in p.lower()
    assert "docs-only" in p.lower()


def test_diff_prompt_targets_an_open_map_changes_branch_when_given_one():
    """R8b-9 / spec §9: while a map-changes PR is open, the AI map-diff session
    commits to THAT branch rather than opening a branch and PR of its own — the
    product's capability proposals all ride one reviewable PR."""
    p = build_map_synthesis_prompt(
        slug="acme", repo="acme/app", default_branch="main",
        open_count=10, closed_count=20, scope="diff",
        suggestion="new-capability", item_numbers=(1, 2),
        branch="pm/map-changes-acme")
    pl = p.lower()
    assert "pm/map-changes-acme" in p
    assert map_branch_name("acme") not in p          # not the historical branch
    assert "do not open" in pl and "pull request" in pl
    # That PR is human-owned from the roadmap UI (Rebase / Abandon) and may
    # carry the human's own in-progress capability edits, so the session must
    # never merge it — the opposite of the branch-of-its-own case below.
    assert "do not arm auto-merge" in pl
    assert "docs-only" in pl and "Co-Authored-By" in p


def test_diff_prompt_without_a_branch_names_the_historical_map_branch():
    """R8b-9's fallback: no open map-changes PR ⇒ the diff session opens its own
    PR on the deterministic ``pm/feature-map-<slug>`` branch."""
    p = build_map_synthesis_prompt(
        slug="acme", repo="acme/app", default_branch="main",
        open_count=10, closed_count=20, scope="diff", suggestion="x")
    assert map_branch_name("acme") in p
    assert "pm/map-changes-acme" not in p
    assert "arm auto-merge" in p.lower()
    assert "do not arm auto-merge" not in p.lower()


def test_initial_and_critic_prompts_still_withhold_auto_merge():
    """The two-phase onboarding pipeline is deliberately NOT changed.

    Synthesis opens the map PR and a coverage critic is chained afterwards to
    add any missing capability docs. If synthesis armed auto-merge the PR could
    merge before that pass runs — and the critic can legitimately never spawn
    (`launch_plan_or_refuse` returns None with no healthy account). Arming there
    would silently drop the anti-omission gate, so it stays a coupled decision.
    """
    initial = build_map_synthesis_prompt(
        slug="acme", repo="acme/app", default_branch="main",
        open_count=10, closed_count=20)
    assert "do not arm auto-merge" in initial.lower()


# --------------------------------------------------------------------------- #
# Shared fenced-JSON extractor (C4-R4): ONE implementation, used by both the   #
# classification (array) and materialization (object) parsers.                 #
# --------------------------------------------------------------------------- #

def test_extract_fenced_json_prefers_the_fenced_block():
    text = 'prose [1, 2] more\n```json\n[{"a": 1}]\n```\ntrailer [3]'
    assert extract_fenced_json(text, shape="array") == [{"a": 1}]


def test_extract_fenced_json_prefers_the_LAST_fence():
    """A session that shows a decoy/example block does it BEFORE its answer.

    The fence the answer lives in is the final one, so a leading block — the
    restated schema, a quoted snippet, a first draft — must never win."""
    text = '''```text
["decoy"]
```
```json
[{"number": 1}]
```'''
    assert extract_fenced_json(text, shape="array") == [{"number": 1}]


def test_parse_classification_output_ignores_a_decoy_fence():
    text = '''```text
[{"number": 99, "kind": "issue", "feature_slug": "wrong", "rationale": "decoy"}]
```
```json
[{"number": 1, "kind": "issue", "feature_slug": "sessions", "rationale": "real"}]
```'''
    (c,) = parse_classification_output(text)
    assert (c.number, c.feature_slug) == (1, "sessions")


def test_extract_fenced_json_falls_back_to_bare_brackets():
    assert extract_fenced_json('chatter [1, 2] tail', shape="array") == [1, 2]
    assert extract_fenced_json('chatter {"a": 1} tail', shape="object") == {"a": 1}


def test_extract_fenced_json_object_shape_ignores_an_array():
    """The object parser must not be fooled by a JSON array in the prose."""
    with pytest.raises(FencedJsonError):
        extract_fenced_json("here is a list: [1, 2, 3]", shape="object")


def test_extract_fenced_json_raises_on_absent_and_on_invalid_json():
    with pytest.raises(FencedJsonError):
        extract_fenced_json("no json at all", shape="array")
    with pytest.raises(FencedJsonError):
        extract_fenced_json("[not, valid, json]", shape="array")
