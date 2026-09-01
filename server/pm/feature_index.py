"""Pure planner: diff parsed feature docs against existing index rows.

The loop reads docs from the clone and Feature rows from the DB, calls this,
and applies the plan. A doc that vanished orphans its row (kept so links never
dangle — spec §4); a doc that reappears revives it.
"""
from __future__ import annotations

from dataclasses import dataclass

from server.pm.featuredoc import FeatureDoc

_SUMMARY_MAX = 200


@dataclass(frozen=True)
class FeatureRowView:
    slug: str
    orphaned: bool


@dataclass(frozen=True)
class FeatureUpsert:
    slug: str
    title: str
    state: str
    summary: str
    repo_path: str


@dataclass(frozen=True)
class FeatureIndexPlan:
    upserts: tuple[FeatureUpsert, ...]
    orphan_slugs: tuple[str, ...]
    revive_slugs: tuple[str, ...]


def _summary(body: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped[:_SUMMARY_MAX]
    return ""


def plan_feature_index(
    existing: list[FeatureRowView],
    docs: list[tuple[str, FeatureDoc]],
) -> FeatureIndexPlan:
    seen = {doc.slug for _, doc in docs}
    upserts = tuple(
        FeatureUpsert(slug=doc.slug, title=doc.title, state=doc.state,
                      summary=_summary(doc.body), repo_path=path)
        for path, doc in docs)
    orphan_slugs = tuple(
        row.slug for row in existing if not row.orphaned and row.slug not in seen)
    revive_slugs = tuple(
        row.slug for row in existing if row.orphaned and row.slug in seen)
    return FeatureIndexPlan(upserts=upserts, orphan_slugs=orphan_slugs,
                            revive_slugs=revive_slugs)
