"""Pure map-changes edits (roadmap spec §8 rows that write Markdown, §14
"frontmatter round-trip")."""
from __future__ import annotations

import pytest

from server.pm.featuredoc import FeatureLinkRef, parse_feature_doc
from server.pm.map_changes_ops import (
    MapContext, MapOp, MapOpError, apply_op, commit_message, validate_op,
)

CTX = MapContext(
    parents={"sessions": None, "dictation": "sessions", "billing": None},
    link_targets=frozenset({("kindash", "sessions"), ("kindash", "dictation"),
                            ("kindash", "billing"), ("web", "auth")}),
)

DOC = """---
slug: dictation
title: Dictation
state: live
parent: sessions
---
# Dictation

Speak instead of typing.

More prose that must survive.
"""


def test_create_writes_a_canonical_doc_at_the_slug_path():
    op = MapOp(op="create", slug="voice-notes", title="Voice notes",
               parent_slug="sessions", summary="Short spoken memos.")
    validate_op(op, CTX)
    path, text = apply_op(op, None)
    assert path == "docs/product/features/voice-notes.md"
    doc = parse_feature_doc(text)
    assert (doc.slug, doc.title, doc.state, doc.parent) == ("voice-notes", "Voice notes", "proposed", "sessions")
    assert doc.body.strip() == "# Voice notes\n\nShort spoken memos."
    assert commit_message(op, "KinDash") == "map(kindash): add capability voice-notes"


def test_create_rejects_bad_slug_taken_slug_and_unknown_parent():
    with pytest.raises(MapOpError) as e:
        validate_op(MapOp(op="create", slug="Voice Notes", title="x"), CTX)
    assert e.value.code == "bad_slug"
    with pytest.raises(MapOpError) as e:
        validate_op(MapOp(op="create", slug="dictation", title="x"), CTX)
    assert e.value.code == "slug_taken"
    with pytest.raises(MapOpError) as e:
        validate_op(MapOp(op="create", slug="new", title="x", parent_slug="nope"), CTX)
    assert e.value.code == "parent_not_found"
    with pytest.raises(MapOpError) as e:
        validate_op(MapOp(op="create", slug="new", title="  "), CTX)
    assert e.value.code == "bad_title"


def test_rename_changes_title_only_and_keeps_the_body():
    op = MapOp(op="rename", slug="dictation", title="Dictation & voice")
    validate_op(op, CTX)
    path, text = apply_op(op, DOC)
    assert path == "docs/product/features/dictation.md"
    doc = parse_feature_doc(text)
    assert doc.title == "Dictation & voice" and doc.parent == "sessions" and doc.state == "live"
    assert "More prose that must survive." in doc.body


def test_reparent_to_root_and_to_a_sibling_but_never_into_a_cycle():
    validate_op(MapOp(op="reparent", slug="dictation", parent_slug=None), CTX)
    _, text = apply_op(MapOp(op="reparent", slug="dictation", parent_slug=None), DOC)
    assert parse_feature_doc(text).parent is None
    validate_op(MapOp(op="reparent", slug="dictation", parent_slug="billing"), CTX)
    with pytest.raises(MapOpError) as e:          # sessions under its own child
        validate_op(MapOp(op="reparent", slug="sessions", parent_slug="dictation"), CTX)
    assert e.value.code == "parent_cycle"
    with pytest.raises(MapOpError) as e:          # self
        validate_op(MapOp(op="reparent", slug="dictation", parent_slug="dictation"), CTX)
    assert e.value.code == "parent_cycle"
    with pytest.raises(MapOpError) as e:
        validate_op(MapOp(op="reparent", slug="ghost", parent_slug=None), CTX)
    assert e.value.code == "not_found"


def test_summary_replaces_the_first_paragraph_and_inserts_one_when_missing():
    op = MapOp(op="summary", slug="dictation", summary="Talk, and it types.")
    validate_op(op, CTX)
    _, text = apply_op(op, DOC)
    body = parse_feature_doc(text).body
    assert body.strip().startswith("# Dictation\n\nTalk, and it types.")
    assert "Speak instead of typing." not in body
    assert "More prose that must survive." in body
    bare = "---\nslug: dictation\ntitle: Dictation\nstate: live\n---\n# Dictation\n"
    _, text = apply_op(op, bare)
    assert parse_feature_doc(text).body.strip() == "# Dictation\n\nTalk, and it types."
    with pytest.raises(MapOpError) as e:
        validate_op(MapOp(op="summary", slug="dictation", summary=" "), CTX)
    assert e.value.code == "bad_summary"


def test_summary_replaces_only_the_first_line_of_a_wrapped_paragraph():
    # The index reads ONE line (`feature_index._summary`); the rest of a wrapped
    # first paragraph is prose that must survive the edit.
    wrapped = DOC.replace("Speak instead of typing.\n", "Speak instead of typing.\nAcross two lines in one paragraph.\n")
    _, text = apply_op(MapOp(op="summary", slug="dictation", summary="Talk, and it types."), wrapped)
    body = parse_feature_doc(text).body
    assert "Talk, and it types.\nAcross two lines in one paragraph." in body
    assert "Speak instead of typing." not in body
    assert "More prose that must survive." in body


def test_links_replace_all_and_targets_must_exist():
    op = MapOp(op="links", slug="dictation", links=(
        FeatureLinkRef(kind="powered_by", project="web", feature="auth"),))
    validate_op(op, CTX)
    _, text = apply_op(op, DOC)
    assert parse_feature_doc(text).links == (FeatureLinkRef("powered_by", "web", "auth"),)
    _, text = apply_op(MapOp(op="links", slug="dictation", links=()), text)
    assert parse_feature_doc(text).links == ()
    with pytest.raises(MapOpError) as e:
        validate_op(MapOp(op="links", slug="dictation", links=(
            FeatureLinkRef(kind="powered_by", project="web", feature="nope"),)), CTX)
    assert e.value.code == "bad_link"
    with pytest.raises(MapOpError) as e:
        validate_op(MapOp(op="links", slug="dictation", links=(
            FeatureLinkRef(kind="depends", project="web", feature="auth"),)), CTX)
    assert e.value.code == "bad_link"


def test_archaeology_dialect_is_canonicalised_on_first_write():
    legacy = "---\ncapability: dictation\nstatus: live\n---\n# Dictation\n\nSpeak.\n"
    _, text = apply_op(MapOp(op="rename", slug="dictation", title="Dictation v2"), legacy)
    assert text.startswith("---\nslug: dictation\ntitle: Dictation v2\nstate: live\n")


def test_unknown_op_and_missing_doc_for_an_edit():
    with pytest.raises(MapOpError) as e:
        validate_op(MapOp(op="delete", slug="dictation"), CTX)
    assert e.value.code == "bad_op"
    with pytest.raises(MapOpError) as e:
        apply_op(MapOp(op="rename", slug="dictation", title="x"), None)
    assert e.value.code == "not_found"


def test_commit_message_names_the_verb_for_every_op():
    """One commit subject shape per op — the branch's log IS the human-readable
    record of what the UI did, and only ``create`` was pinned."""
    assert [commit_message(MapOp(op=op, slug="dictation"), "KinDash")
            for op in ("create", "rename", "reparent", "summary", "links")] == [
        "map(kindash): add capability dictation",
        "map(kindash): rename capability dictation",
        "map(kindash): move capability dictation",
        "map(kindash): update summary of dictation",
        "map(kindash): update links of dictation",
    ]
    # A multi-word product name is slugified into the scope.
    assert commit_message(MapOp(op="rename", slug="x"), "Example Product").startswith(
        "map(example-product): ")
