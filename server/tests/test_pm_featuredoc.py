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
