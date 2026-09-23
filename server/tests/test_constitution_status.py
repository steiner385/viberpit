"""Behaviour of the onboarded-repo constitution classifier (``server.sdlc.constitution_status``).

Read-only hygiene classification, distinct from the amendment gate next door — see
``test_constitution_gate.py`` for the enforcement half.
"""
from pathlib import Path

from server.sdlc.constitution_status import classify_document


def _doc(version: str, amended: str) -> str:
    return (
        "# ViberPit Constitution\n\nBody.\n\n"
        f"**Version**: {version} | **Ratified**: 2026-06-19 | **Last Amended**: {amended}"
    )


def test_classify_document_none_is_missing():
    assert classify_document(None) == "missing"


def test_classify_document_empty_is_missing():
    assert classify_document("   \n") == "missing"


def test_classify_document_spec_kit_template_is_template():
    template = (
        "# [PROJECT_NAME] Constitution\n\n"
        "## Core Principles\n\n"
        "### [PRINCIPLE_1_NAME]\n\n"
        "**Version**: [CONSTITUTION_VERSION] | **Ratified**: [RATIFICATION_DATE] | "
        "**Last Amended**: [LAST_AMENDED_DATE]"
    )
    assert classify_document(template) == "template"


def test_classify_document_missing_footer_is_no_version_footer():
    text = "# Real Constitution\n\n## Core Principles\n\nSome real, non-template prose."
    assert classify_document(text) == "no_version_footer"


def test_classify_document_ratified_document_is_none():
    assert classify_document(_doc("1.0.0", "2026-01-01")) is None


def test_classify_document_accepts_the_real_constitution():
    """ViberPit's own ratified constitution must never misclassify as a template —
    it contains bracketed prose (e.g. inline citations), so the placeholder regex
    must not be so broad it catches real writing."""
    text = (Path(__file__).resolve().parents[2] / ".specify/memory/constitution.md").read_text()
    assert classify_document(text) is None
