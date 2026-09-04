"""Pure SDLC review-policy engine (Phase 4, issue #806).

All tests are I/O-free. Covers: policy parsing, finding classification, path
exclusion, comment-command detection, Initiative PR detection, and CLAUDE.md
seeding decisions.
"""
from __future__ import annotations

import pytest

from server.sdlc.review_policy import (
    ALLOWED_CATEGORIES,
    CLAUDE_MD_SEED_THRESHOLD,
    DEFAULT_NIT_CAP,
    REVIEW_MD_PATH,
    SEED_MIN_SEVERITY,
    SEVERITY_TAGS,
    FindingRecord,
    ReviewPolicy,
    build_claude_md_entry,
    classify_severity,
    extract_at_claude_command,
    is_excluded_path,
    is_initiative_pr,
    parse_review_md,
    severity_gte,
    should_seed_claude_md,
)


# --------------------------------------------------------------------------- #
# ReviewPolicy defaults                                                         #
# --------------------------------------------------------------------------- #

def test_default_policy_categories():
    p = ReviewPolicy()
    assert set(p.categories) == set(ALLOWED_CATEGORIES)


def test_default_policy_severity_tags():
    p = ReviewPolicy()
    assert set(p.severity_tags) == set(SEVERITY_TAGS)


def test_default_policy_nit_cap():
    p = ReviewPolicy()
    assert p.nit_cap == DEFAULT_NIT_CAP


def test_review_md_path_constant():
    # Single source of truth for the file location.
    assert REVIEW_MD_PATH == ".claude/REVIEW.md"


# --------------------------------------------------------------------------- #
# parse_review_md                                                               #
# --------------------------------------------------------------------------- #

def test_parse_review_md_extracts_nit_cap():
    text = "A review **must not** post more than **5 `[nit]`** findings per PR."
    policy = parse_review_md(text)
    assert policy.nit_cap == 5


def test_parse_review_md_uses_default_nit_cap_when_absent():
    policy = parse_review_md("No nit cap mentioned here.")
    assert policy.nit_cap == DEFAULT_NIT_CAP


def test_parse_review_md_ignores_unknown_content():
    text = "Some random content\nwith no structured data\n**3 `[nit]`** per PR."
    policy = parse_review_md(text)
    assert policy.nit_cap == 3


def test_parse_review_md_picks_first_nit_cap():
    text = "**2 `[nit]`** per PR.\n**5 `[nit]`** also mentioned."
    policy = parse_review_md(text)
    assert policy.nit_cap == 2


def test_parse_review_md_with_real_review_md_content():
    text = (
        "# ccswitch PR Review Policy\n\n"
        "A review **must not** post more than **3 `[nit]`** findings per PR.\n"
    )
    policy = parse_review_md(text)
    assert policy.nit_cap == 3


def test_parse_review_md_empty_string():
    policy = parse_review_md("")
    assert policy.nit_cap == DEFAULT_NIT_CAP


# --------------------------------------------------------------------------- #
# classify_severity                                                             #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("tag", ["critical", "major", "minor", "nit"])
def test_classify_severity_recognizes_bracketed_tags(tag):
    assert classify_severity(f"[{tag}] Something is wrong here.") == tag


@pytest.mark.parametrize("tag", ["critical", "major", "minor", "nit"])
def test_classify_severity_recognizes_bare_tags(tag):
    assert classify_severity(f"Severity: {tag} — this is bad") == tag


def test_classify_severity_is_case_insensitive():
    assert classify_severity("[CRITICAL] SQL injection") == "critical"
    assert classify_severity("[Major] off-by-one") == "major"


def test_classify_severity_defaults_to_minor_when_no_tag():
    assert classify_severity("Something might be wrong") == "minor"


def test_classify_severity_returns_first_tag():
    # "minor" appears before "nit" — must return "minor".
    assert classify_severity("[minor] small thing [nit] also this") == "minor"


# --------------------------------------------------------------------------- #
# severity_gte                                                                  #
# --------------------------------------------------------------------------- #

def test_severity_gte_same_tag():
    assert severity_gte("critical", "critical")
    assert severity_gte("nit", "nit")


def test_severity_gte_more_severe_than_less():
    assert severity_gte("critical", "major")
    assert severity_gte("major", "minor")
    assert severity_gte("minor", "nit")


def test_severity_gte_less_severe_fails():
    assert not severity_gte("nit", "minor")
    assert not severity_gte("minor", "major")
    assert not severity_gte("major", "critical")


def test_severity_gte_unknown_treated_as_minor():
    assert severity_gte("critical", "unknown")
    assert not severity_gte("nit", "unknown")


# --------------------------------------------------------------------------- #
# is_excluded_path                                                              #
# --------------------------------------------------------------------------- #

def test_is_excluded_path_matches_ui_component():
    p = ReviewPolicy()
    assert is_excluded_path("web/src/components/ui/button.tsx", p)


def test_is_excluded_path_matches_dist():
    p = ReviewPolicy()
    assert is_excluded_path("web/dist/index.js", p)


def test_is_excluded_path_matches_venv():
    p = ReviewPolicy()
    assert is_excluded_path(".venv/lib/python3.12/site-packages/foo.py", p)


def test_is_excluded_path_matches_lock_file():
    p = ReviewPolicy()
    assert is_excluded_path("uv.lock", p)


def test_is_excluded_path_does_not_match_normal_source():
    p = ReviewPolicy()
    assert not is_excluded_path("server/sdlc/review_policy.py", p)
    assert not is_excluded_path("web/src/components/SessionCard.tsx", p)


def test_is_excluded_path_with_custom_pattern():
    p = ReviewPolicy(exclude_patterns=("custom/generated/*",))
    assert is_excluded_path("custom/generated/foo.py", p)
    assert not is_excluded_path("server/foo.py", p)


# --------------------------------------------------------------------------- #
# extract_at_claude_command                                                     #
# --------------------------------------------------------------------------- #

def test_extract_at_claude_command_basic_fix():
    cmd = extract_at_claude_command("@claude fix the null check in line 42")
    assert cmd is not None
    assert cmd.verb == "fix"
    assert "null check" in cmd.text


def test_extract_at_claude_command_with_leading_whitespace():
    cmd = extract_at_claude_command("  @claude fix add missing type annotation")
    assert cmd is not None
    assert cmd.verb == "fix"


def test_extract_at_claude_command_case_insensitive():
    cmd = extract_at_claude_command("@Claude Fix this")
    assert cmd is not None
    assert cmd.verb == "fix"


def test_extract_at_claude_command_in_multiline_comment():
    body = "Thanks for the PR!\n\n@claude fix the off-by-one in the loop\n\nLooks good otherwise."
    cmd = extract_at_claude_command(body)
    assert cmd is not None
    assert cmd.verb == "fix"
    assert "off-by-one" in cmd.text


def test_extract_at_claude_command_no_command():
    assert extract_at_claude_command("LGTM!") is None
    assert extract_at_claude_command("") is None
    assert extract_at_claude_command("Mention @claude without a verb?") is None


def test_extract_at_claude_command_preserves_raw():
    body = "@claude fix this thing"
    cmd = extract_at_claude_command(body)
    assert cmd is not None
    assert cmd.raw == body


def test_extract_at_claude_command_verb_only():
    cmd = extract_at_claude_command("@claude fix")
    assert cmd is not None
    assert cmd.verb == "fix"
    assert cmd.text == ""


# --------------------------------------------------------------------------- #
# is_initiative_pr                                                              #
# --------------------------------------------------------------------------- #

def test_is_initiative_pr_by_label():
    assert is_initiative_pr(labels=["sdlc", "enhancement"])
    assert is_initiative_pr(labels=["sdlc-artifact"])
    assert is_initiative_pr(labels=["initiative"])


def test_is_initiative_pr_by_title_prefix():
    assert is_initiative_pr(title="intent: capture the auth rewrite trigger")
    assert is_initiative_pr(title="spec: auth rewrite specification")
    assert is_initiative_pr(title="plan: auth rewrite implementation plan")
    assert is_initiative_pr(title="[sdlc] some initiative PR")
    assert is_initiative_pr(title="[intent] capture auth rewrite")


def test_is_initiative_pr_by_title_case_insensitive():
    assert is_initiative_pr(title="Intent: something")
    assert is_initiative_pr(title="SPEC: something")


def test_is_initiative_pr_by_changed_files():
    assert is_initiative_pr(changed_files=["docs/sdlc/auth-rewrite/intent.md"])
    assert is_initiative_pr(changed_files=["docs/sdlc/my-slug/spec.md"])
    assert is_initiative_pr(changed_files=["docs/sdlc/foo/plan.md"])


def test_is_initiative_pr_not_matching():
    assert not is_initiative_pr(title="fix: auth null check", labels=[], changed_files=[])
    assert not is_initiative_pr(title="feat: new endpoint")
    assert not is_initiative_pr(changed_files=["server/sdlc/engine.py"])


def test_is_initiative_pr_empty():
    assert not is_initiative_pr()


# --------------------------------------------------------------------------- #
# FindingRecord + should_seed_claude_md                                         #
# --------------------------------------------------------------------------- #

def test_finding_record_counts_distinct_prs():
    r = FindingRecord("null-check-missing")
    r.record(101)
    r.record(102)
    r.record(101)  # duplicate — must not double-count
    assert r.occurrence_count == 2


def test_should_seed_claude_md_major_at_threshold():
    r = FindingRecord("null-check-missing")
    for pr in range(CLAUDE_MD_SEED_THRESHOLD):
        r.record(pr + 100)
    assert should_seed_claude_md(r, "major")


def test_should_seed_claude_md_critical_at_threshold():
    r = FindingRecord("sql-injection")
    for pr in range(CLAUDE_MD_SEED_THRESHOLD):
        r.record(pr + 100)
    assert should_seed_claude_md(r, "critical")


def test_should_seed_claude_md_below_threshold():
    r = FindingRecord("null-check-missing")
    r.record(100)
    assert not should_seed_claude_md(r, "major")


def test_should_seed_claude_md_nit_never_seeds():
    r = FindingRecord("trailing-space")
    for pr in range(10):
        r.record(pr + 100)
    assert not should_seed_claude_md(r, "nit")


def test_should_seed_claude_md_minor_never_seeds():
    r = FindingRecord("minor-edge-case")
    for pr in range(10):
        r.record(pr + 100)
    assert not should_seed_claude_md(r, "minor")


def test_seed_min_severity_is_major():
    assert SEED_MIN_SEVERITY == "major"


# --------------------------------------------------------------------------- #
# build_claude_md_entry                                                         #
# --------------------------------------------------------------------------- #

def test_build_claude_md_entry_format():
    r = FindingRecord("null-check-missing")
    r.record(101)
    r.record(102)
    entry = build_claude_md_entry(
        r,
        description="Forgot to check for None before indexing",
        fix="Add `if x is None: return` guard before the indexing line",
    )
    assert "[null-check-missing]" in entry
    assert "PR #101" in entry
    assert "PR #102" in entry
    assert "None" in entry


def test_build_claude_md_entry_single_pr():
    r = FindingRecord("sql-injection")
    r.record(99)
    entry = build_claude_md_entry(r, description="Raw SQL concat", fix="Use parameterised query")
    assert "PR #99" in entry
    assert "sql-injection" in entry
