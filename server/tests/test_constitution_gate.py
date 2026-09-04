"""Behaviour of the constitution amendment gate (``server.sdlc.constitution_gate``).

The gate is the only enforcement layer that survives a new tool, a shell idiom,
or a human editing on github.com — it inspects the resulting diff rather than
guessing how the diff was produced. These cases pin each of its four checks.
"""
from __future__ import annotations

import pytest

from server.sdlc.constitution_gate import (
    ACKNOWLEDGMENT_MARKER,
    check_amendment,
    check_guardrail_change,
    parse_footer,
)
from server.sdlc.governance import GUARDRAIL_PATHS

_ACK = "Owner acknowledgment: I approve the provider-neutral changes."


def _doc(version: str, amended: str, report: str = "") -> str:
    return (
        f"<!--\nSYNC IMPACT REPORT\n{report}\n-->\n\n"
        "# ccswitch Constitution\n\nBody.\n\n"
        f"**Version**: {version} | **Ratified**: 2026-06-19 | **Last Amended**: {amended}"
    )


BASE = _doc("1.1.0", "2026-06-19")
HEAD_OK = _doc("1.2.0", "2026-09-04", "Version change: 1.1.0 → 1.2.0   [amendment]")


def test_clean_amendment_passes():
    assert check_amendment(BASE, HEAD_OK, _ACK) == []


def test_acknowledgment_is_case_insensitive():
    assert check_amendment(BASE, HEAD_OK, "OWNER ACKNOWLEDGMENT: approved") == []


# --------------------------------------------------------------------------- #
# What does NOT count as an acknowledgment                                     #
# --------------------------------------------------------------------------- #

def test_acknowledgment_inside_an_html_comment_does_not_count():
    """A template that *asks* for the line must not satisfy itself.

    Found the hard way: PR #1111's instruction comment quoted the marker verbatim
    while telling the author to add the real line, and a naive substring match
    passed the check on a body that had no acknowledgment at all.
    """
    body = (
        "<!-- REQUIRED: replace the line below with your acknowledgment.\n"
        '     It must begin with "Owner acknowledgment:" — the check greps for it.\n'
        "     -->\n\n**[ TODO — acknowledgment goes here ]**\n\nSome description."
    )
    violations = check_amendment(BASE, HEAD_OK, body)
    assert any(ACKNOWLEDGMENT_MARKER in v for v in violations)
    assert check_guardrail_change(["server/sdlc/governance.py"], body) != []


def test_marker_buried_mid_sentence_does_not_count():
    body = "I was told the owner acknowledgment: prefix is what the gate looks for."
    assert check_amendment(BASE, HEAD_OK, body) != []


def test_bare_marker_with_no_statement_does_not_count():
    assert check_amendment(BASE, HEAD_OK, "Owner acknowledgment:") != []
    assert check_amendment(BASE, HEAD_OK, "Owner acknowledgment:   \n") != []


@pytest.mark.parametrize(
    "line",
    [
        "Owner acknowledgment: I approve these changes.",
        "**Owner acknowledgment:** I approve these changes.",
        "> Owner acknowledgment: I approve these changes.",
        "## Owner acknowledgment: I approve these changes.",
        "  Owner acknowledgment: I approve these changes.",
    ],
)
def test_acknowledgment_line_shapes_that_do_count(line):
    body = f"Some preamble.\n\n{line}\n\nMore description."
    assert check_amendment(BASE, HEAD_OK, body) == []


def test_acknowledgment_after_a_comment_block_counts():
    body = (
        "<!-- instructions mentioning Owner acknowledgment: here -->\n\n"
        "Owner acknowledgment: I approve the provider-neutral changes.\n"
    )
    assert check_amendment(BASE, HEAD_OK, body) == []


def test_missing_acknowledgment_is_rejected():
    violations = check_amendment(BASE, HEAD_OK, "Looks good to me.")
    assert any(ACKNOWLEDGMENT_MARKER in v for v in violations)


def test_empty_pr_body_is_rejected():
    assert check_amendment(BASE, HEAD_OK, "") != []


def test_unbumped_version_is_rejected():
    head = _doc("1.1.0", "2026-09-04", "Version change: 1.1.0 → 1.1.0")
    violations = check_amendment(BASE, head, _ACK)
    assert any("version not bumped" in v for v in violations)


def test_downgraded_version_is_rejected():
    head = _doc("1.0.0", "2026-09-04", "Version change: 1.1.0 → 1.0.0")
    assert any("version not bumped" in v for v in check_amendment(BASE, head, _ACK))


def test_missing_sync_impact_entry_is_rejected():
    head = _doc("1.2.0", "2026-09-04", "no transition recorded here")
    violations = check_amendment(BASE, head, _ACK)
    assert any("Sync Impact Report" in v for v in violations)


def test_stale_last_amended_is_rejected():
    head = _doc("1.2.0", "2026-06-19", "Version change: 1.1.0 → 1.2.0")
    violations = check_amendment(BASE, head, _ACK)
    assert any("Last Amended is unchanged" in v for v in violations)


def test_malformed_head_footer_short_circuits():
    violations = check_amendment(BASE, "# Constitution\n\nno footer", _ACK)
    assert violations == ["the Version / Ratified / Last Amended footer is missing or malformed"]


def test_violations_accumulate():
    """A PR can fail several checks at once; report all of them, not the first."""
    head = _doc("1.1.0", "2026-06-19", "nothing recorded")
    violations = check_amendment(BASE, head, "no acknowledgment")
    assert len(violations) == 4


@pytest.mark.parametrize(
    "version,amended", [("1.2.0", "2026-09-04"), ("2.0.0", "2026-12-31")]
)
def test_parse_footer_roundtrip(version, amended):
    parsed = parse_footer(_doc(version, amended))
    assert parsed == {"version": version, "ratified": "2026-06-19", "amended": amended}


def test_parse_footer_returns_none_without_a_footer():
    assert parse_footer("# Constitution\n\nbody only") is None


# --------------------------------------------------------------------------- #
# Guardrail changes need the acknowledgment, but not a version bump             #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("path", GUARDRAIL_PATHS)
def test_guardrail_change_without_acknowledgment_is_rejected(path):
    violations = check_guardrail_change([path], "just a small tweak")
    assert violations, f"{path} changed without acknowledgment was allowed"
    assert path in violations[0]


@pytest.mark.parametrize("path", GUARDRAIL_PATHS)
def test_guardrail_change_with_acknowledgment_passes(path):
    assert check_guardrail_change([path], _ACK) == []


def test_ordinary_change_needs_no_acknowledgment():
    """The gate runs on every PR; it must stay silent on unrelated diffs."""
    ordinary = ["server/app.py", "web/src/main.tsx", "README.md", "tests/test_ledger.py"]
    assert check_guardrail_change(ordinary, "") == []


def test_empty_diff_passes():
    assert check_guardrail_change([], "") == []


def test_multiple_guardrails_are_all_listed():
    violations = check_guardrail_change(
        ["server/sdlc/governance.py", ".github/CODEOWNERS", "server/app.py"], "",
    )
    assert len(violations) == 1
    assert "server/sdlc/governance.py" in violations[0]
    assert ".github/CODEOWNERS" in violations[0]
    assert "server/app.py" not in violations[0]


def test_guardrail_paths_all_exist_in_the_repo():
    """A typo'd path would silently protect nothing."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    missing = [p for p in GUARDRAIL_PATHS if not (root / p).exists()]
    assert not missing, f"GUARDRAIL_PATHS names files that do not exist: {missing}"


def test_real_constitution_footer_parses():
    """Guard against the gate silently no-opping if the footer format drifts."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    text = (root / ".specify/memory/constitution.md").read_text()
    parsed = parse_footer(text)
    assert parsed is not None, "the committed constitution's footer no longer parses"
    assert parsed["version"].count(".") == 2
