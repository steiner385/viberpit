from datetime import datetime, timedelta, timezone

from server.pm.health import roadmap_health

NOW = datetime(2026, 9, 12, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=45)
NEW = NOW - timedelta(days=3)


def test_health_flags_loose_leaves_unlinked_leaf_initiatives_stale_proposed_and_old_inbox():
    caps = [
        ("tasks", None, "live", NEW), ("task-views", "tasks", "delivering", NEW),
        ("flaky-test-triage", None, "proposed", OLD),          # root with no children while a tree exists
        ("ai-assistant-chat", "ai", "proposed", NEW),          # proposed but fresh
    ]
    inits = [("audit", 0, True, False), ("audit-01", 0, False, False), ("views", 1, False, False),
            ("retired", 0, False, True)]   # done and unlinked — finished, not drifting
    props = [OLD, NEW]
    h = roadmap_health(capabilities=caps, initiatives=inits, proposals=props, now=NOW)
    assert h.unparented_capabilities == ("flaky-test-triage",)
    assert h.unlinked_initiatives == ("audit-01",)          # a program is judged by its phases
    assert "retired" not in h.unlinked_initiatives          # done initiatives are excluded
    assert h.stale_proposed == ("flaky-test-triage",)
    assert h.stale_inbox == 1


def test_health_of_a_flat_map_flags_nothing_as_unparented():
    caps = [("a", None, "live", NEW), ("b", None, "live", NEW)]
    h = roadmap_health(capabilities=caps, initiatives=[], proposals=[], now=NOW)
    assert h.unparented_capabilities == ()


def test_health_surfaces_unindexed_docs_sorted():
    h = roadmap_health(
        capabilities=[], initiatives=[], proposals=[], now=NOW,
        unindexed_docs=[("docs/product/features/z.md", "bad"),
                        ("docs/product/features/a.md", "unterminated fence")])
    assert h.unindexed_docs == (
        ("docs/product/features/a.md", "unterminated fence"),
        ("docs/product/features/z.md", "bad"))


def test_health_unindexed_docs_defaults_to_empty():
    h = roadmap_health(capabilities=[], initiatives=[], proposals=[], now=NOW)
    assert h.unindexed_docs == ()


def test_health_passes_through_governance_status():
    h = roadmap_health(capabilities=[], initiatives=[], proposals=[], now=NOW,
                       governance="template")
    assert h.governance == "template"


def test_health_governance_defaults_to_none():
    h = roadmap_health(capabilities=[], initiatives=[], proposals=[], now=NOW)
    assert h.governance is None


def test_health_flags_artifact_with_closed_epic_on_a_not_done_initiative():
    inits = [("proj-a", 1, False, False)]  # not done
    artifacts = [("proj-a", True, "o/r#1", ())]
    h = roadmap_health(capabilities=[], initiatives=inits, proposals=[], now=NOW,
                       artifacts=artifacts, epic_state={"o/r#1": "closed"})
    assert h.artifacts_epic_closed_not_done == ("proj-a",)


def test_health_does_not_flag_closed_epic_when_initiative_is_done():
    inits = [("proj-a", 1, False, True)]  # done
    artifacts = [("proj-a", True, "o/r#1", ())]
    h = roadmap_health(capabilities=[], initiatives=inits, proposals=[], now=NOW,
                       artifacts=artifacts, epic_state={"o/r#1": "closed"})
    assert h.artifacts_epic_closed_not_done == ()


def test_health_does_not_flag_an_open_epic():
    inits = [("proj-a", 1, False, False)]
    artifacts = [("proj-a", True, "o/r#1", ())]
    h = roadmap_health(capabilities=[], initiatives=inits, proposals=[], now=NOW,
                       artifacts=artifacts, epic_state={"o/r#1": "open"})
    assert h.artifacts_epic_closed_not_done == ()


def test_health_flags_capability_named_by_frontmatter_but_not_indexed():
    artifacts = [("proj-a", True, None, ("ghost-capability",))]
    h = roadmap_health(capabilities=[("real-cap", None, "live", NEW)], initiatives=[],
                       proposals=[], now=NOW, artifacts=artifacts)
    assert h.artifacts_unknown_capability == (("proj-a", "ghost-capability"),)


def test_health_does_not_flag_an_indexed_capability():
    artifacts = [("proj-a", True, None, ("real-cap",))]
    h = roadmap_health(capabilities=[("real-cap", None, "live", NEW)], initiatives=[],
                       proposals=[], now=NOW, artifacts=artifacts)
    assert h.artifacts_unknown_capability == ()


def test_health_flags_initiative_with_committed_artifact_but_no_frontmatter():
    artifacts = [("proj-a", False, None, ())]
    h = roadmap_health(capabilities=[], initiatives=[], proposals=[], now=NOW, artifacts=artifacts)
    assert h.artifacts_missing_frontmatter == ("proj-a",)


def test_health_never_checked_frontmatter_is_not_flagged_missing():
    # None = never successfully read yet, distinct from a confirmed absence.
    artifacts = [("proj-a", None, None, ())]
    h = roadmap_health(capabilities=[], initiatives=[], proposals=[], now=NOW, artifacts=artifacts)
    assert h.artifacts_missing_frontmatter == ()


def test_health_artifact_fields_default_to_empty():
    h = roadmap_health(capabilities=[], initiatives=[], proposals=[], now=NOW)
    assert h.artifacts_epic_closed_not_done == ()
    assert h.artifacts_unknown_capability == ()
    assert h.artifacts_missing_frontmatter == ()


def test_health_accepts_naive_utc_timestamps():
    # Every DB column is `DateTime(timezone=True)`, naive UTC on SQLite (the
    # dialect has no native timezone type and hands the value back
    # tzinfo-less) but tz-AWARE on Postgres, which really does apply the
    # column definition. This is the SQLite path under test: the production
    # code compares a naive `updated_at` / `created_at` against the aware
    # `now` this function receives.
    NAIVE_OLD = (NOW - timedelta(days=45)).replace(tzinfo=None)
    NAIVE_NEW = (NOW - timedelta(days=3)).replace(tzinfo=None)
    caps = [("old", None, "proposed", NAIVE_OLD), ("root", None, "live", NAIVE_NEW),
            ("kid", "root", "live", NAIVE_NEW)]
    h = roadmap_health(capabilities=caps, initiatives=[],
                       proposals=[NAIVE_OLD, NAIVE_NEW, None], now=NOW)
    assert h.stale_proposed == ("old",)
    assert h.stale_inbox == 1
    assert h.unparented_capabilities == ("old",)


def test_health_flags_unlinked_persona_slug():
    h = roadmap_health(
        capabilities=[("chores", None, "live", NEW)], initiatives=[], proposals=[], now=NOW,
        capability_personas=[("chores", ("teen", "ghost"))], known_personas=["teen"])
    assert h.unlinked_personas == (("chores", "ghost"),)


def test_health_unlinked_personas_defaults_to_empty():
    h = roadmap_health(capabilities=[], initiatives=[], proposals=[], now=NOW)
    assert h.unlinked_personas == ()


def test_health_no_false_positive_when_all_personas_resolve():
    h = roadmap_health(
        capabilities=[("chores", None, "live", NEW)], initiatives=[], proposals=[], now=NOW,
        capability_personas=[("chores", ("teen",))], known_personas=["teen"])
    assert h.unlinked_personas == ()
