"""Pure planner: diff parsed feature docs against existing index rows.

The loop reads docs from the clone and Feature rows from the DB, calls this,
and applies the plan. A doc that vanished orphans its row (kept so links never
dangle — spec §4); a doc that reappears revives it.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from server.pm.featuredoc import FeatureDoc, FeatureLinkRef

_SUMMARY_MAX = 200


@dataclass(frozen=True)
class FeatureRowView:
    slug: str
    orphaned: bool


@dataclass(frozen=True)
class FeatureDocsRead:
    """One atomic read of ``docs/product/features/*.md`` at some ref: the
    commit it was read at, plus the (path, text) pairs found there. ``sha`` is
    what a non-proposed doc's ``FeatureUpsert.commit_sha`` is stamped with
    (R8b-4) — the InitiativeArtifact pointer pattern applied to Feature rows."""

    sha: str
    docs: list[tuple[str, str]]


@dataclass(frozen=True)
class FeatureUpsert:
    slug: str
    title: str
    state: str
    summary: str
    repo_path: str
    parent_slug: str | None
    links: tuple[FeatureLinkRef, ...]
    # R8b-4: a doc proposed on the map-changes branch gets pr_number/pr_url and
    # commit_sha=None; a doc read off the default branch gets commit_sha (the
    # SHA it was read at) and no PR fields. Defaulted so every pre-existing
    # caller keeps constructing a plain, unstamped upsert.
    commit_sha: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None


@dataclass(frozen=True)
class FeatureIndexPlan:
    upserts: tuple[FeatureUpsert, ...]
    orphan_slugs: tuple[str, ...]
    revive_slugs: tuple[str, ...]
    warnings: tuple[str, ...]


def _summary(body: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped[:_SUMMARY_MAX]
    return ""


def _resolve_parents(docs: list[tuple[str, FeatureDoc]]) -> tuple[dict[str, str | None], list[str]]:
    """Map slug → parent slug, dropping unknown parents and breaking cycles.

    Docs are visited in the order given; a cycle is broken at the FIRST doc
    (in that order) whose parent chain returns to itself, so the outcome is
    deterministic for a given doc listing."""
    parent: dict[str, str | None] = {}
    warnings: list[str] = []
    slugs = {doc.slug for _, doc in docs}
    for _, doc in docs:
        if doc.parent is None:
            parent[doc.slug] = None
        elif doc.parent not in slugs:
            parent[doc.slug] = None
            warnings.append(f"{doc.slug}: parent '{doc.parent}' not found; indexed at root")
        else:
            parent[doc.slug] = doc.parent
    for _, doc in docs:
        seen = {doc.slug}
        cur = parent.get(doc.slug)
        while cur is not None:
            if cur in seen:
                parent[doc.slug] = None
                warnings.append(f"{doc.slug}: parent chain is a cycle; indexed at root")
                break
            seen.add(cur)
            cur = parent.get(cur)
    return parent, warnings


def plan_feature_index(
    existing: list[FeatureRowView],
    docs: list[tuple[str, FeatureDoc]],
    *,
    read_sha: str | None = None,
    proposed: Mapping[str, tuple[int | None, str | None]] | None = None,
) -> FeatureIndexPlan:
    """*read_sha* is the commit *docs* were read at (default branch); *proposed*
    maps a slug to ``(pr_number, pr_url)`` for every doc that is only proposed
    on a map-changes branch (R8b-4). A doc in *proposed* is stamped with those
    PR fields and ``commit_sha=None``; every other doc is stamped with
    ``commit_sha=read_sha`` and no PR fields. Both kwargs default to ``None``
    so an omitted call keeps today's behaviour (no PR fields, no commit_sha)."""
    seen = {doc.slug for _, doc in docs}
    parent_of, warnings = _resolve_parents(docs)
    proposed_map = proposed or {}
    upserts = tuple(
        FeatureUpsert(
            slug=doc.slug, title=doc.title, state=doc.state,
            summary=_summary(doc.body), repo_path=path,
            parent_slug=parent_of.get(doc.slug), links=doc.links,
            commit_sha=None if doc.slug in proposed_map else read_sha,
            pr_number=proposed_map[doc.slug][0] if doc.slug in proposed_map else None,
            pr_url=proposed_map[doc.slug][1] if doc.slug in proposed_map else None,
        )
        for path, doc in docs)
    orphan_slugs = tuple(
        row.slug for row in existing if not row.orphaned and row.slug not in seen)
    revive_slugs = tuple(
        row.slug for row in existing if row.orphaned and row.slug in seen)
    return FeatureIndexPlan(upserts=upserts, orphan_slugs=orphan_slugs,
                            revive_slugs=revive_slugs, warnings=tuple(warnings))
