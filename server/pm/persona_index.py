"""Pure planner: diff parsed persona docs against existing Persona rows
(issue #2164). Mirrors the non-evidence, non-map-changes-branch subset of
``server/pm/feature_index.py`` — personas are default-branch-only."""
from __future__ import annotations

from dataclasses import dataclass

from server.pm.personadoc import PersonaDoc

_SUMMARY_MAX = 200


@dataclass(frozen=True)
class PersonaRowView:
    slug: str
    orphaned: bool


@dataclass(frozen=True)
class PersonaUpsert:
    slug: str
    title: str
    role: str | None
    age_band: str | None
    tier: str | None
    summary: str
    repo_path: str


@dataclass(frozen=True)
class PersonaIndexPlan:
    upserts: tuple[PersonaUpsert, ...]
    orphan_slugs: tuple[str, ...]
    revive_slugs: tuple[str, ...]


def _summary(body: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped[:_SUMMARY_MAX]
    return ""


def plan_persona_index(
    existing: list[PersonaRowView], docs: list[tuple[str, PersonaDoc]],
) -> PersonaIndexPlan:
    seen = {doc.slug for _, doc in docs}
    by_slug: dict[str, tuple[str, PersonaDoc]] = {}
    for path, doc in docs:
        by_slug[doc.slug] = (path, doc)  # last one wins — mirrors plan_feature_index
    upserts = tuple(
        PersonaUpsert(slug=doc.slug, title=doc.title, role=doc.role, age_band=doc.age_band,
                     tier=doc.tier, summary=_summary(doc.body), repo_path=path)
        for path, doc in by_slug.values())
    orphan_slugs = tuple(
        row.slug for row in existing if not row.orphaned and row.slug not in seen)
    revive_slugs = tuple(
        row.slug for row in existing if row.orphaned and row.slug in seen)
    return PersonaIndexPlan(upserts=upserts, orphan_slugs=orphan_slugs, revive_slugs=revive_slugs)
