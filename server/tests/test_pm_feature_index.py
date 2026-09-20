"""Pure feature-index planner tests (spec §4: upsert + sunset-orphan)."""
import pathlib

import yaml

from server.pm.feature_index import FeatureRowView, plan_feature_index
from server.pm.featuredoc import FeatureDoc, FeatureDocError, FeatureLinkRef, parse_feature_doc

FEATURE_DOCS_DIR = pathlib.Path(__file__).parents[2] / "docs" / "product" / "features"


def _doc(slug: str, body: str = "## Problem\n\nSummary line here.") -> FeatureDoc:
    return FeatureDoc(slug=slug, title=slug.title(), state="live", links=(), body=body)


def _linked(kind: str, project: str, feature: str, slug: str = "a") -> FeatureDoc:
    return FeatureDoc(slug=slug, title=slug.title(), state="live", body="x",
                      links=(FeatureLinkRef(kind=kind, project=project, feature=feature),))


def feature_doc_edge_problems(
    docs: list[tuple[str, FeatureDoc]], slugs: set[str]
) -> list[str]:
    """Every parent/link edge in *docs* that this checkout cannot resolve.

    Resolved PER EDGE. The predecessor gated the link half on a repo-wide
    ``len({link.project for ...}) == 1`` flag, so a single doc whose
    ``project:`` had drifted made the set size two and switched the link check
    off for every doc at once — the #1879 failure mode, in which the guard went
    dark on exactly the drift it was added to catch and took an unrelated
    dangling edge down with it.

    The one-project precondition is therefore an OUTPUT now, not a switch: a
    disagreement is reported, and every feature target is still resolved
    against this repo either way. When a genuinely cross-project link lands,
    that report is the prompt to teach this function which project slugs are
    external — a call a human should make once, in the open, rather than one
    the guard makes silently on every run.
    """
    problems: list[str] = []
    for name, doc in docs:
        if doc.parent and doc.parent not in slugs:
            problems.append(f"{name}: parent '{doc.parent}'")
        problems += [f"{name}: {link.kind} -> {link.project}/{link.feature}"
                     for link in doc.links if link.feature not in slugs]
    projects = sorted({link.project for _, doc in docs for link in doc.links})
    if len(projects) > 1:
        problems.append(
            f"docs name more than one project ({', '.join(projects)}); every link's "
            "project must be this repo's own product slug until this guard is taught "
            "which ones are external")
    return problems


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


def test_a_drifted_project_slug_is_reported_instead_of_disabling_the_guard():
    """#1879: the guard must not go dark on the very drift it exists to catch.

    The check below used to gate the whole link half on a repo-wide
    ``len({link.project for ...}) == 1``. That flag is computed ACROSS docs, so
    one doc whose ``project:`` still named the pre-rebrand product made the set
    size two and switched the link check off for *every* doc — silently, and
    precisely in the state the guard is meant to flag. #1879 is that shape: one
    stale ``project:``, 520 runtime warnings, and a green CI guard.

    A second project slug is now a finding in its own right, and the per-edge
    feature check keeps running regardless.
    """
    docs = [
        ("command-line-interface.md", _linked("integrates_with", "old-product-slug",
                                              "multi-account-management")),
        ("config-dir-isolation.md", _linked("powered_by", "viberpit",
                                            "multi-account-management")),
    ]
    slugs = {"command-line-interface", "config-dir-isolation", "multi-account-management"}
    problems = feature_doc_edge_problems(docs, slugs)
    assert any("old-product-slug" in p and "viberpit" in p for p in problems), problems


def test_an_unverifiable_link_does_not_silence_a_sibling_edge():
    """The regression pin: one bad edge must cost exactly one edge.

    Under the repo-wide flag this pair reported nothing at all — the odd
    ``project:`` disabled the check, so the genuinely dangling target in the
    *other* doc sailed through too. Both are reported now.
    """
    docs = [
        ("a.md", _linked("integrates_with", "old-product-slug", "b")),
        ("b.md", _linked("powered_by", "viberpit", "does-not-exist")),
    ]
    problems = feature_doc_edge_problems(docs, {"a", "b"})
    assert any("does-not-exist" in p for p in problems), problems


def test_a_clean_single_project_doc_set_has_no_problems():
    docs = [("a.md", _linked("powered_by", "viberpit", "b")), ("b.md", _doc("b"))]
    assert feature_doc_edge_problems(docs, {"a", "b"}) == []


def test_an_unknown_parent_is_reported():
    doc = FeatureDoc(slug="a", title="A", state="live", links=(), body="x", parent="nope")
    assert feature_doc_edge_problems([("a.md", doc)], {"a"}) == ["a.md: parent 'nope'"]


def test_every_checked_in_feature_doc_targets_a_slug_this_repo_has():
    """A dangling intra-repo target logs on every pm-sync tick, forever (#1878).

    ``_pm_sync_apply_feature_plan`` skips an unresolvable link with a warning,
    and ``_resolve_parents`` re-roots an unknown parent with one — both by
    design, since ViberPit indexes repos whose docs it cannot gate and a bad
    edge must never fail the sync. The cost is that the warning has no
    terminating condition: it fires again next tick, and the tick after, until
    someone edits the doc. #1878 is two days of exactly that, #1879 another
    520 warnings off one stale ``project:``. Runtime stays forgiving; CI holds
    *our* docs to a target that exists.
    """
    docs = [(p.name, parse_feature_doc(p.read_text(), path=f"docs/product/features/{p.name}"))
            for p in sorted(FEATURE_DOCS_DIR.glob("*.md"))]
    slugs = {p.stem for p in FEATURE_DOCS_DIR.glob("*.md")}
    assert feature_doc_edge_problems(docs, slugs) == []
