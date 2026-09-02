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


def test_evidence_dict_links_are_tolerated():
    """links written as an evidence dict (code/prs keys) must not fail the doc."""
    doc = parse_feature_doc(ARCHAEOLOGY_DOC)
    assert doc.slug == "session-manager"
    assert doc.links == ()  # non-list links → treated as no links


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


def test_non_dict_link_item_is_skipped():
    """A string or scalar in the links list must be skipped, not fail the doc."""
    text = (
        "---\n"
        "slug: a\ntitle: A\nstate: proposed\n"
        "links:\n"
        "  - kind: powered_by\n    project: p\n    feature: f\n"
        "  - just-a-string\n"
        "---\nbody\n"
    )
    doc = parse_feature_doc(text)
    assert len(doc.links) == 1
