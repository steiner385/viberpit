import pytest

from server.pm.personadoc import PersonaDoc, PersonaDocError, parse_persona_doc


def test_parses_full_frontmatter():
    text = (
        "---\n"
        "slug: teen\n"
        "title: Teen\n"
        "role: teen\n"
        "age_band: 13-17\n"
        "tier: standard\n"
        "---\n"
        "# Teen\n\nGoals: independence with guardrails.\n"
    )
    doc = parse_persona_doc(text)
    assert doc == PersonaDoc(
        slug="teen", title="Teen", role="teen", age_band="13-17", tier="standard",
        body="# Teen\n\nGoals: independence with guardrails.\n",
    )


def test_missing_optional_fields_default_to_none():
    doc = parse_persona_doc("---\nslug: guest\ntitle: Guest\n---\nbody\n")
    assert doc.role is None
    assert doc.age_band is None
    assert doc.tier is None


def test_title_falls_back_to_first_h1():
    doc = parse_persona_doc("---\nslug: viewer\n---\n# Viewer\n\nbody\n")
    assert doc.title == "Viewer"


def test_no_fence_falls_back_to_path_slug():
    doc = parse_persona_doc("Just a body, no fence.\n", path="docs/product/personas/kiosk.md")
    assert doc.slug == "kiosk"
    assert doc.title == ""


def test_no_fence_and_no_resolvable_path_raises():
    with pytest.raises(PersonaDocError):
        parse_persona_doc("Just a body, no fence.\n")


def test_missing_slug_and_title_raises():
    with pytest.raises(PersonaDocError):
        parse_persona_doc("---\nrole: teen\n---\nbody\n")


def test_unterminated_fence_raises():
    with pytest.raises(PersonaDocError):
        parse_persona_doc("---\nslug: a\n")


def test_bad_yaml_raises():
    with pytest.raises(PersonaDocError):
        parse_persona_doc("---\nslug: [unclosed\n---\nbody\n")


def test_invalid_slug_from_path_is_not_indexed():
    with pytest.raises(PersonaDocError):
        parse_persona_doc("body only\n", path="docs/product/personas/README.md")
