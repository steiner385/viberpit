"""Pure feature-index planner tests (spec §4: upsert + sunset-orphan)."""
from server.pm.feature_index import FeatureRowView, plan_feature_index
from server.pm.featuredoc import FeatureDoc


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
