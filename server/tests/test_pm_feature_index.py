"""Pure feature-index planner tests (spec §4: upsert + sunset-orphan)."""
import pathlib

import yaml

from server.pm.feature_index import FeatureRowView, plan_feature_index
from server.pm.featuredoc import FeatureDoc, FeatureDocError, FeatureLinkRef, parse_feature_doc

FEATURE_DOCS_DIR = pathlib.Path(__file__).parents[2] / "docs" / "product" / "features"


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


def test_every_checked_in_feature_doc_is_canonical():
    """Guards #1634 and keeps its teeth after #1874/#1903 widened the reader.

    The reader now rescues a doc with no frontmatter (slug from the path) or a
    lifecycle synonym (``in-progress`` → ``delivering``), because ViberPit
    indexes repos whose docs it cannot gate. "It parses" therefore no longer
    proves OUR docs follow the convention — so CI holds this repo's docs to
    exactly what ``serialize_feature_doc`` writes: a real fence, canonical
    ``slug``/``state`` keys, canonical values, and slug == filename.
    """
    problems = {}
    for path in sorted(FEATURE_DOCS_DIR.glob("*.md")):
        text = path.read_text()
        try:
            doc = parse_feature_doc(text, path=f"docs/product/features/{path.name}")
        except FeatureDocError as e:
            problems[path.name] = str(e)
            continue
        if not text.startswith("---"):
            problems[path.name] = "no YAML frontmatter fence"
            continue
        meta = yaml.safe_load(text.split("---", 2)[1]) or {}
        if meta.get("slug") != path.stem:
            problems[path.name] = f"frontmatter slug {meta.get('slug')!r} != filename"
        elif meta.get("state") != doc.state:
            problems[path.name] = (
                f"state {meta.get('state')!r} is not the canonical {doc.state!r}")
    assert not problems


def test_every_checked_in_feature_doc_targets_a_slug_this_repo_has():
    """A dangling intra-repo target logs on every pm-sync tick, forever (#1878).

    ``_pm_sync_apply_feature_plan`` skips an unresolvable link with a warning,
    and ``_resolve_parents`` re-roots an unknown parent with one — both by
    design, since ViberPit indexes repos whose docs it cannot gate and a bad
    edge must never fail the sync. The cost is that the warning has no
    terminating condition: it fires again next tick, and the tick after, until
    someone edits the doc. #1878 is two days of exactly that. Runtime stays
    forgiving; CI holds *our* docs to a target that exists.

    Scope is what is checkable from a checkout. A ``parent`` is always
    intra-repo. A link is only checkable while this repo's docs all name one
    project — their own — which is the case today; the first genuine
    cross-project link makes the target another repo's business and turns the
    link half off deliberately. The half #1878 actually tripped on — a
    ``project:`` naming the pre-rebrand slug, left behind by the rename —
    belongs to the brand ratchet, which is what swept it.
    """
    docs = [(p.name, parse_feature_doc(p.read_text(), path=f"docs/product/features/{p.name}"))
            for p in sorted(FEATURE_DOCS_DIR.glob("*.md"))]
    slugs = {p.stem for p in FEATURE_DOCS_DIR.glob("*.md")}
    intra_repo = len({link.project for _, doc in docs for link in doc.links}) == 1
    dangling = []
    for name, doc in docs:
        if doc.parent and doc.parent not in slugs:
            dangling.append(f"{name}: parent '{doc.parent}'")
        if intra_repo:
            dangling += [f"{name}: {link.kind} -> {link.project}/{link.feature}"
                         for link in doc.links if link.feature not in slugs]
    assert not dangling
