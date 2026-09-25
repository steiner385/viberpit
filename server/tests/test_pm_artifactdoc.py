"""SDLC artifact frontmatter parser tests (issue #2161)."""
import pytest

from server.pm.artifactdoc import ArtifactDocError, parse_artifact_doc

VALID = """---
initiative: task-views
kind: spec
epic: steiner385/viberpit#100
capabilities: [task-views, task-detail]
upstream: [specs/007-shared-task-model/spec.md]
verified_against: abc123
---
## Problem

Users can't see task views.
"""


def test_parse_valid_doc():
    doc = parse_artifact_doc(VALID)
    assert doc.initiative == "task-views"
    assert doc.kind == "spec"
    assert doc.epic == "steiner385/viberpit#100"
    assert doc.capabilities == ("task-views", "task-detail")
    assert doc.upstream == ("specs/007-shared-task-model/spec.md",)
    assert doc.verified_against == "abc123"
    assert doc.body.startswith("## Problem")


def test_no_frontmatter_falls_back_to_caller_supplied_identity():
    doc = parse_artifact_doc("just a markdown body, no fences", slug="my-init", kind="intent")
    assert doc.initiative == "my-init"
    assert doc.kind == "intent"
    assert doc.epic is None
    assert doc.capabilities == ()
    assert doc.body == "just a markdown body, no fences"


def test_no_frontmatter_and_no_caller_identity_raises():
    with pytest.raises(ArtifactDocError):
        parse_artifact_doc("just a markdown body, no fences")


def test_unterminated_fence_raises():
    with pytest.raises(ArtifactDocError):
        parse_artifact_doc("---\ninitiative: a\nkind: intent\n")


def test_bad_yaml_raises():
    with pytest.raises(ArtifactDocError):
        parse_artifact_doc("---\n[unclosed\n---\nbody\n")


def test_unknown_kind_raises():
    text = "---\ninitiative: a\nkind: bogus\n---\nbody\n"
    with pytest.raises(ArtifactDocError):
        parse_artifact_doc(text)


def test_bad_epic_shape_raises():
    text = "---\ninitiative: a\nkind: intent\nepic: not-a-ref\n---\nbody\n"
    with pytest.raises(ArtifactDocError):
        parse_artifact_doc(text)


def test_optional_fields_absent_by_default():
    text = "---\ninitiative: a\nkind: intent\n---\nbody\n"
    doc = parse_artifact_doc(text)
    assert doc.epic is None
    assert doc.capabilities == ()
    assert doc.upstream == ()
    assert doc.verified_against is None


def test_capabilities_accepts_a_lone_string():
    text = "---\ninitiative: a\nkind: spec\ncapabilities: solo-cap\n---\nbody\n"
    doc = parse_artifact_doc(text)
    assert doc.capabilities == ("solo-cap",)


def test_frontmatter_initiative_kind_override_caller_supplied_values():
    text = "---\ninitiative: real-slug\nkind: plan\n---\nbody\n"
    doc = parse_artifact_doc(text, slug="wrong-slug", kind="intent")
    assert doc.initiative == "real-slug"
    assert doc.kind == "plan"
