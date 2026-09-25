from server.pm.persona_index import PersonaRowView, plan_persona_index
from server.pm.personadoc import PersonaDoc


def _doc(slug: str, body: str = "## Goals\n\nIndependence with guardrails.") -> PersonaDoc:
    return PersonaDoc(slug=slug, title=slug.title(), role=None, age_band=None, tier=None, body=body)


def test_upserts_every_doc_with_summary_from_first_body_line():
    plan = plan_persona_index([], [("docs/product/personas/teen.md", _doc("teen"))])
    assert len(plan.upserts) == 1
    up = plan.upserts[0]
    assert up.slug == "teen"
    assert up.title == "Teen"
    assert up.repo_path == "docs/product/personas/teen.md"
    assert up.summary == "Independence with guardrails."


def test_missing_doc_orphans_existing_row():
    existing = [PersonaRowView(slug="teen", orphaned=False)]
    plan = plan_persona_index(existing, [])
    assert plan.orphan_slugs == ("teen",)
    assert plan.revive_slugs == ()


def test_reappearing_doc_revives_orphaned_row():
    existing = [PersonaRowView(slug="teen", orphaned=True)]
    plan = plan_persona_index(existing, [("docs/product/personas/teen.md", _doc("teen"))])
    assert plan.revive_slugs == ("teen",)
    assert plan.orphan_slugs == ()


def test_untouched_row_is_neither_orphaned_nor_revived():
    existing = [PersonaRowView(slug="teen", orphaned=False)]
    plan = plan_persona_index(existing, [("docs/product/personas/teen.md", _doc("teen"))])
    assert plan.orphan_slugs == ()
    assert plan.revive_slugs == ()


def test_duplicate_slug_across_docs_does_not_raise_last_one_wins():
    docs = [("docs/product/personas/teen-1.md", _doc("teen", "First.")),
            ("docs/product/personas/teen-2.md", _doc("teen", "Second."))]
    plan = plan_persona_index([], docs)
    assert len(plan.upserts) == 1
    assert plan.upserts[0].summary == "Second."
