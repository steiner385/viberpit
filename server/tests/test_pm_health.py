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
    inits = [("audit", 0, True), ("audit-01", 0, False), ("views", 1, False)]
    props = [OLD, NEW]
    h = roadmap_health(capabilities=caps, initiatives=inits, proposals=props, now=NOW)
    assert h.unparented_capabilities == ("flaky-test-triage",)
    assert h.unlinked_initiatives == ("audit-01",)          # a program is judged by its phases
    assert h.stale_proposed == ("flaky-test-triage",)
    assert h.stale_inbox == 1


def test_health_of_a_flat_map_flags_nothing_as_unparented():
    caps = [("a", None, "live", NEW), ("b", None, "live", NEW)]
    h = roadmap_health(capabilities=caps, initiatives=[], proposals=[], now=NOW)
    assert h.unparented_capabilities == ()
