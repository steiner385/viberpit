"""Feature-doc frontmatter parser tests (spec §3 'feature doc convention')."""
import pytest

from server.pm.featuredoc import (
    FeatureDocError,
    FeatureLinkRef,
    parse_feature_doc,
    serialize_feature_doc,
)

VALID = """---
slug: notifications
title: Notifications
state: live
links:
  - kind: powered_by
    project: infra
    feature: message-queue
---
## Problem

Users miss important events.
"""


def test_parse_valid_doc():
    doc = parse_feature_doc(VALID)
    assert doc.slug == "notifications"
    assert doc.state == "live"
    assert doc.links == (FeatureLinkRef("powered_by", "infra", "message-queue"),)
    assert doc.body.startswith("## Problem")


def test_round_trip_is_stable():
    doc = parse_feature_doc(VALID)
    assert parse_feature_doc(serialize_feature_doc(doc)) == doc


def test_missing_frontmatter_raises():
    with pytest.raises(FeatureDocError):
        parse_feature_doc("just a markdown body, no fences")


def test_bad_state_raises():
    with pytest.raises(FeatureDocError):
        parse_feature_doc(VALID.replace("state: live", "state: bogus"))


def test_links_optional():
    text = "---\nslug: a\ntitle: A\nstate: proposed\n---\nbody\n"
    assert parse_feature_doc(text).links == ()


# Regression: archaeology-authored docs use links as an evidence dict, not a list.
# parse_feature_doc must tolerate this and yield 0 links rather than raising.
ARCHAEOLOGY_DOC = """---
slug: session-manager
title: Session Manager
state: live
links:
  code:
    - src/ccswitch/accounts.py
    - src/ccswitch/sessions.py
  prs:
    - '#352'
    - '#873'
---
## Problem

Users need to run multiple Claude accounts.

## Evidence

See src/ccswitch/accounts.py, PRs #352, #873.
"""


def test_archaeology_links_dict_does_not_raise():
    """parse_feature_doc must not raise when links is an evidence dict
    (archaeology shape: {code: [...], prs: [...]}).  The whole doc must be
    returned with slug/title/state/body intact and links safely empty."""
    doc = parse_feature_doc(ARCHAEOLOGY_DOC)
    assert doc.slug == "session-manager"
    assert doc.title == "Session Manager"
    assert doc.state == "live"
    assert doc.body.startswith("## Problem")
    # The archaeology evidence dict contains no valid FeatureLinkRef items — links empty.
    assert doc.links == ()


def test_valid_feature_links_still_parse():
    """A proper links list must still produce FeatureLinkRef objects after the fix."""
    doc = parse_feature_doc(VALID)
    assert doc.links == (FeatureLinkRef("powered_by", "infra", "message-queue"),)


def test_unknown_link_kind_is_skipped():
    """An unknown link kind must be skipped, not fail the whole doc."""
    text = (
        "---\n"
        "slug: a\ntitle: A\nstate: proposed\n"
        "links:\n"
        "  - kind: powered_by\n    project: p\n    feature: f\n"
        "  - kind: unknown_kind\n    project: p\n    feature: g\n"
        "---\nbody\n"
    )
    doc = parse_feature_doc(text)
    assert len(doc.links) == 1
    assert doc.links[0].kind == "powered_by"


def test_links_list_with_malformed_item_skips_bad_keeps_good():
    """A links list mixing valid and malformed items must skip the bad ones."""
    text = (
        "---\nslug: a\ntitle: A\nstate: proposed\nlinks:\n"
        "  - kind: powered_by\n    project: p\n    feature: f\n"
        "  - not-a-mapping\n"  # scalar — should be skipped
        "  - kind: unknown_kind\n    feature: x\n"  # bad kind — should be skipped
        "---\nbody\n"
    )
    doc = parse_feature_doc(text)
    assert len(doc.links) == 1
    assert doc.links[0] == FeatureLinkRef("powered_by", "p", "f")


WITH_PARENT = """---
slug: server-side-stt
title: Server-side STT
state: delivering
parent: dictation
---
## Problem

Whisper on the edge.
"""


def test_parse_parent_slug():
    doc = parse_feature_doc(WITH_PARENT)
    assert doc.parent == "dictation"


def test_parse_missing_or_blank_parent_is_root():
    assert parse_feature_doc(VALID).parent is None
    blank = VALID.replace("state: live\n", "state: live\nparent: '  '\n")
    assert parse_feature_doc(blank).parent is None


def test_parent_equal_to_self_is_rejected():
    bad = WITH_PARENT.replace("parent: dictation", "parent: server-side-stt")
    with pytest.raises(FeatureDocError):
        parse_feature_doc(bad)


def test_serialize_round_trips_parent():
    doc = parse_feature_doc(WITH_PARENT)
    text = serialize_feature_doc(doc)
    assert "parent: dictation" in text
    assert parse_feature_doc(text).parent == "dictation"
    # A root doc must not gain a `parent:` line.
    assert "parent:" not in serialize_feature_doc(parse_feature_doc(VALID))


# --- Alias dialect ---------------------------------------------------------- #
# Agent-authored docs use `capability:`/`status:` and carry the title only as an
# H1, because the prompt that asks for a feature doc describes the body sections
# and never the frontmatter. Reading is wider than writing: a silent skip leaves
# the capability off the map, which un-latches the pm-triage drift branch and
# re-proposes it forever.

ALIAS = """---
capability: flaky-test-triage
status: proposed
area: ci-infrastructure
owner: engineering
---

# Flaky test triage

## Problem

Stale re-detections cost a triage session each.
"""


def test_capability_and_status_aliases_parse():
    doc = parse_feature_doc(ALIAS)
    assert doc.slug == "flaky-test-triage"
    assert doc.state == "proposed"


def test_title_falls_back_to_first_h1():
    assert parse_feature_doc(ALIAS).title == "Flaky test triage"


def test_canonical_keys_win_over_aliases():
    text = ("---\nslug: real\ncapability: alias\ntitle: Real\n"
            "state: live\nstatus: proposed\n---\n# Ignored\nbody\n")
    doc = parse_feature_doc(text)
    assert (doc.slug, doc.title, doc.state) == ("real", "Real", "live")


def test_explicit_title_wins_over_h1():
    text = "---\nslug: a\ntitle: From Frontmatter\nstate: proposed\n---\n# From H1\n"
    assert parse_feature_doc(text).title == "From Frontmatter"


def test_no_title_and_no_h1_still_raises():
    with pytest.raises(FeatureDocError):
        parse_feature_doc("---\ncapability: a\nstatus: proposed\n---\nno heading here\n")


def test_h2_is_not_a_title():
    # Only `# ` counts; a doc whose first heading is `##` has no title.
    with pytest.raises(FeatureDocError):
        parse_feature_doc("---\ncapability: a\nstatus: proposed\n---\n## Problem\n")


def test_alias_doc_round_trips_into_canonical_form():
    # serialize always writes the canonical keys, so a re-parse is stable.
    doc = parse_feature_doc(ALIAS)
    assert parse_feature_doc(serialize_feature_doc(doc)) == doc


def test_bad_state_via_status_alias_still_raises():
    with pytest.raises(FeatureDocError):
        parse_feature_doc(ALIAS.replace("status: proposed", "status: bogus"))
