"""Pure feature-index planner tests (spec §4: upsert + sunset-orphan)."""
from server.pm.feature_index import FeatureRowView, plan_feature_index
from server.pm.featuredoc import FeatureDoc, FeatureLinkRef, parse_feature_doc


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
    link = FeatureLinkRef("powered_by", "viberpit", "sessions")
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


# -- R8b-4: commit_sha / pr_number stamping ------------------------------------


def test_omitted_kwargs_stamp_nothing_today_unchanged():
    plan = plan_feature_index([], [("d/a.md", _doc("a"))])
    up = plan.upserts[0]
    assert up.commit_sha is None
    assert up.pr_number is None
    assert up.pr_url is None


def test_plain_doc_is_stamped_with_the_read_sha():
    plan = plan_feature_index([], [("d/a.md", _doc("a"))], read_sha="sha-main")
    up = plan.upserts[0]
    assert up.commit_sha == "sha-main"
    assert up.pr_number is None
    assert up.pr_url is None


def test_proposed_doc_is_stamped_with_pr_fields_and_no_commit_sha():
    plan = plan_feature_index(
        [], [("d/a.md", _doc("a")), ("d/b.md", _doc("b"))],
        read_sha="sha-main",
        proposed={"a": (12, "https://github.com/o/r/pull/12")})
    by_slug = {u.slug: u for u in plan.upserts}
    assert by_slug["a"].commit_sha is None
    assert by_slug["a"].pr_number == 12
    assert by_slug["a"].pr_url == "https://github.com/o/r/pull/12"
    assert by_slug["b"].commit_sha == "sha-main"
    assert by_slug["b"].pr_number is None
    assert by_slug["b"].pr_url is None


def test_labels_are_carried_onto_the_upsert():
    doc = parse_feature_doc(
        "---\nslug: flaky-test-triage\ntitle: Flaky\nstate: live\nlabels: [flaky]\n---\nbody\n")
    plan = plan_feature_index([], [("docs/product/features/flaky-test-triage.md", doc)])
    assert plan.upserts[0].labels == ("flaky",)
