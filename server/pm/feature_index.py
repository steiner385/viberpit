"""Pure planner: diff parsed feature docs against existing index rows.

The loop reads docs from the clone and Feature rows from the DB, calls this,
and applies the plan. A doc that vanished orphans its row (kept so links never
dangle — spec §4); a doc that reappears revives it.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from server.pm.evidence import PathResolution
from server.pm.featuredoc import FeatureDoc, FeatureDocError, FeatureLinkRef, parse_feature_doc

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
class EvidenceUpsert:
    """One cited path (#2162). ``missing``/``last_commit_at`` are ``None`` when
    this pass could not resolve it — the apply keeps the last resolved value
    rather than forgetting it."""

    kind: str
    path: str
    missing: bool | None = None
    last_commit_at: datetime | None = None


@dataclass(frozen=True)
class FeatureUpsert:
    slug: str
    title: str
    state: str
    summary: str
    repo_path: str
    parent_slug: str | None
    links: tuple[FeatureLinkRef, ...]
    labels: tuple[str, ...] = ()
    personas: tuple[str, ...] = ()
    # R8b-4: a doc proposed on the map-changes branch gets pr_number/pr_url and
    # commit_sha=None; a doc read off the default branch gets commit_sha (the
    # SHA it was read at) and no PR fields. Defaulted so every pre-existing
    # caller keeps constructing a plain, unstamped upsert.
    commit_sha: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    evidence: tuple[EvidenceUpsert, ...] = ()
    doc_committed_at: datetime | None = None


@dataclass(frozen=True)
class PlanWarning:
    """A doc the planner indexed at root because its parent could not stand.

    Structured rather than a preformatted line so the apply can log it as one
    event per *kind* with the slugs as fields (#2243): interpolated slugs made
    every broken parent its own auto-error fingerprint, re-filed every tick."""

    kind: str  # "parent_unresolved" | "parent_cycle"
    slug: str
    parent: str | None

    @property
    def detail(self) -> str:
        if self.kind == "parent_cycle":
            return "parent chain is a cycle; indexed at root"
        return f"parent '{self.parent}' not found; indexed at root"

    def __str__(self) -> str:
        return f"{self.slug}: {self.detail}"


@dataclass(frozen=True)
class FeatureIndexPlan:
    upserts: tuple[FeatureUpsert, ...]
    orphan_slugs: tuple[str, ...]
    revive_slugs: tuple[str, ...]
    warnings: tuple[PlanWarning, ...]


def _summary(body: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped[:_SUMMARY_MAX]
    return ""


def _resolve_parents(
    docs: list[tuple[str, FeatureDoc]],
) -> tuple[dict[str, str | None], list[PlanWarning]]:
    """Map slug → parent slug, dropping unknown parents and breaking cycles.

    Docs are visited in the order given; a cycle is broken at the FIRST doc
    (in that order) whose parent chain returns to itself, so the outcome is
    deterministic for a given doc listing."""
    parent: dict[str, str | None] = {}
    warnings: list[PlanWarning] = []
    slugs = {doc.slug for _, doc in docs}
    for _, doc in docs:
        if doc.parent is None:
            parent[doc.slug] = None
        elif doc.parent not in slugs:
            parent[doc.slug] = None
            warnings.append(PlanWarning("parent_unresolved", doc.slug, doc.parent))
        else:
            parent[doc.slug] = doc.parent
    for _, doc in docs:
        seen = {doc.slug}
        cur = parent.get(doc.slug)
        while cur is not None:
            if cur in seen:
                parent[doc.slug] = None
                warnings.append(PlanWarning("parent_cycle", doc.slug, doc.parent))
                break
            seen.add(cur)
            cur = parent.get(cur)
    return parent, warnings


def _evidence_upsert(kind: str, path: str,
                     res: Mapping[str, PathResolution]) -> EvidenceUpsert:
    r = res.get(path)
    if r is None:
        return EvidenceUpsert(kind=kind, path=path)
    return EvidenceUpsert(kind=kind, path=path, missing=not r.exists,
                          last_commit_at=r.last_commit_at)


def merge_branch_over_main(
    main_docs: list[tuple[str, str]],
    branch_docs: list[tuple[str, str]],
    *, pr_number: int | None, pr_url: str | None,
) -> tuple[list[tuple[str, FeatureDoc]], dict[str, tuple[int | None, str | None]], list[str]]:
    """Parse every default-branch doc, then override by slug with any branch
    doc that differs from what the branch merely inherited (R8b-4, mirrored
    from the inline block in ``PmSyncLoop._feature_index``,
    ``server/automations/pm_sync.py:654-694`` — see that method for the
    throttled-logging/feed-event variant of this same decision used by the
    background loop).

    A branch inherits every default-branch doc, so the branch read returns all
    of them — stamping the whole set "proposed" would null every untouched
    capability's ``commit_sha`` and hand it this PR's number, so abandoning
    the PR would delete the entire index. An unchanged inherited doc is
    therefore skipped: it keeps its default-branch identity untouched.

    Returns ``(docs, proposed, skipped)``: ``docs`` is ready for
    ``plan_feature_index``, ``proposed`` maps a proposed slug to
    ``(pr_number, pr_url)``, ``skipped`` is one ``"path: message"`` string per
    doc (from either side) that failed to parse, for the caller to log.
    """
    docs_by_slug: dict[str, tuple[str, FeatureDoc]] = {}
    main_text_by_slug: dict[str, str] = {}
    skipped: list[str] = []
    for path, text in main_docs:
        try:
            doc = parse_feature_doc(text, path=path)
        except FeatureDocError as e:
            skipped.append(f"{path}: {e}")
            continue
        docs_by_slug[doc.slug] = (path, doc)
        main_text_by_slug[doc.slug] = text

    proposed: dict[str, tuple[int | None, str | None]] = {}
    for path, text in branch_docs:
        try:
            doc = parse_feature_doc(text, path=path)
        except FeatureDocError as e:
            skipped.append(f"{path}: {e}")
            continue
        if main_text_by_slug.get(doc.slug) == text:
            continue  # inherited unchanged — not this PR's doing
        docs_by_slug[doc.slug] = (path, doc)
        proposed[doc.slug] = (pr_number, pr_url)

    return list(docs_by_slug.values()), proposed, skipped


def plan_feature_index(
    existing: list[FeatureRowView],
    docs: list[tuple[str, FeatureDoc]],
    *,
    read_sha: str | None = None,
    proposed: Mapping[str, tuple[int | None, str | None]] | None = None,
    resolution: Mapping[str, PathResolution] | None = None,
) -> FeatureIndexPlan:
    """*read_sha* is the commit *docs* were read at (default branch); *proposed*
    maps a slug to ``(pr_number, pr_url)`` for every doc that is only proposed
    on a map-changes branch (R8b-4). A doc in *proposed* is stamped with those
    PR fields and ``commit_sha=None``; every other doc is stamped with
    ``commit_sha=read_sha`` and no PR fields. Both kwargs default to ``None``
    so an omitted call keeps today's behaviour (no PR fields, no commit_sha).

    *resolution* maps a cited evidence path — and each doc's own repo path — to
    how it resolved against the default-branch tree (#2162). A path it does not
    cover is left unresolved (``missing=None``), never assumed present."""
    seen = {doc.slug for _, doc in docs}
    parent_of, warnings = _resolve_parents(docs)
    proposed_map = proposed or {}
    res = resolution or {}
    upserts = tuple(
        FeatureUpsert(
            slug=doc.slug, title=doc.title, state=doc.state,
            summary=_summary(doc.body), repo_path=path,
            parent_slug=parent_of.get(doc.slug), links=doc.links, labels=doc.labels,
            personas=doc.personas,
            commit_sha=None if doc.slug in proposed_map else read_sha,
            pr_number=proposed_map[doc.slug][0] if doc.slug in proposed_map else None,
            pr_url=proposed_map[doc.slug][1] if doc.slug in proposed_map else None,
            evidence=tuple(_evidence_upsert(e.kind, e.path, res) for e in doc.evidence),
            doc_committed_at=res[path].last_commit_at if path in res else None,
        )
        for path, doc in docs)
    orphan_slugs = tuple(
        row.slug for row in existing if not row.orphaned and row.slug not in seen)
    revive_slugs = tuple(
        row.slug for row in existing if row.orphaned and row.slug in seen)
    return FeatureIndexPlan(upserts=upserts, orphan_slugs=orphan_slugs,
                            revive_slugs=revive_slugs, warnings=tuple(warnings))
