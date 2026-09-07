"""Pure feature-index planner tests (spec §4: upsert + sunset-orphan)."""
from server.pm.feature_index import FeatureRowView, plan_feature_index
from server.pm.featuredoc import FeatureDoc, FeatureLinkRef


def _doc(slug: str, body: str = "## Problem\n\nSummary line here.") -> FeatureDoc:
    return FeatureDoc(slug=slug, title=slug.title(), state="live", links=(), body=body)


def test_new_docs_become_upserts():
    plan = plan_feature_index([], [("docs/product/features/a.md", _doc("a"))])
    assert len(plan.upserts) == 1
    up = plan.upserts[0]
    assert (up.slug, up.state, up.repo_path) == ("a", "live", "docs/product/features/a.md")
    assert up.summary == "Summary line here."


def test_missing_doc_orphans_row_and_reappearing_revives():
    existing = [FeatureRowView(slug="gone", orphaned=False),
                FeatureRowView(slug="back", orphaned=True)]
    plan = plan_feature_index(existing, [("docs/product/features/back.md", _doc("back"))])
    assert plan.orphan_slugs == ("gone",)
    assert plan.revive_slugs == ("back",)


def test_already_orphaned_not_reorphaned():
    existing = [FeatureRowView(slug="gone", orphaned=True)]
    plan = plan_feature_index(existing, [])
    assert plan.orphan_slugs == ()


def _pdoc(slug: str, parent: str | None, links=()) -> FeatureDoc:
    return FeatureDoc(slug=slug, title=slug.title(), state="live", links=tuple(links),
                      body="## Problem\n\nx", parent=parent)


def test_parent_and_links_are_carried_onto_the_upsert():
    link = FeatureLinkRef("powered_by", "ccswitch", "sessions")
    plan = plan_feature_index([], [
        ("d/sessions.md", _pdoc("sessions", None)),
        ("d/dictation.md", _pdoc("dictation", "sessions", [link])),
    ])
    by_slug = {u.slug: u for u in plan.upserts}
    assert by_slug["sessions"].parent_slug is None
    assert by_slug["dictation"].parent_slug == "sessions"
    assert by_slug["dictation"].links == (link,)
    assert plan.warnings == ()


def test_unknown_parent_indexes_at_root_with_a_warning():
    plan = plan_feature_index([], [("d/a.md", _pdoc("a", "nope"))])
    assert plan.upserts[0].parent_slug is None
    assert plan.warnings == ("a: parent 'nope' not found; indexed at root",)


def test_parent_cycle_breaks_at_the_closing_doc():
    plan = plan_feature_index([], [
        ("d/a.md", _pdoc("a", "c")),
        ("d/b.md", _pdoc("b", "a")),
        ("d/c.md", _pdoc("c", "b")),
    ])
    by_slug = {u.slug: u for u in plan.upserts}
    roots = [s for s, u in by_slug.items() if u.parent_slug is None]
    # Exactly one doc is cut loose, and the other two keep their parents.
    assert len(roots) == 1
    assert len(plan.warnings) == 1
    assert plan.warnings[0].endswith("parent chain is a cycle; indexed at root")
