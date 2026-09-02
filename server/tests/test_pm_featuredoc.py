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
