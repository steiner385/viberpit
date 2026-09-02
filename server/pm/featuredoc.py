"""Feature-doc convention: parse/serialize ``docs/product/features/<slug>.md``.

YAML frontmatter carries the machine-readable index fields; the markdown body
is the human artifact (spec §3). Pure functions, no I/O — the sync loop feeds
file text in and upserts Feature rows from the result.
"""
from __future__ import annotations

from dataclasses import dataclass

import yaml

from server.db.models import FEATURE_LIFECYCLE_STATES, FEATURE_LINK_KINDS


class FeatureDocError(ValueError):
    """Raised for a malformed feature doc. Callers log and skip — one bad doc
    must never poison the whole index (spec §4)."""


@dataclass(frozen=True)
class FeatureLinkRef:
    kind: str
    project: str
    feature: str


@dataclass(frozen=True)
class FeatureDoc:
    slug: str
    title: str
    state: str
    links: tuple[FeatureLinkRef, ...]
    body: str


def parse_feature_doc(text: str) -> FeatureDoc:
    if not text.startswith("---"):
        raise FeatureDocError("missing frontmatter fence")
    lines = text.splitlines()
    try:
        close = next(i for i, ln in enumerate(lines[1:], start=1) if ln.strip() == "---")
    except StopIteration:
        raise FeatureDocError("unterminated frontmatter fence") from None
    raw = "\n".join(lines[1:close])
    try:
        meta = yaml.safe_load(raw) or {}
    except yaml.YAMLError as e:
        raise FeatureDocError(f"bad yaml frontmatter: {e}") from e
    if not isinstance(meta, dict):
        raise FeatureDocError("frontmatter is not a mapping")

    slug = str(meta.get("slug") or "").strip()
    title = str(meta.get("title") or "").strip()
    state = str(meta.get("state") or "").strip()
    if not slug or not title:
        raise FeatureDocError("slug and title are required")
    if state not in FEATURE_LIFECYCLE_STATES:
        raise FeatureDocError(f"state {state!r} not in {FEATURE_LIFECYCLE_STATES}")

    links: list[FeatureLinkRef] = []
    raw_links = meta.get("links")
    # Tolerate an archaeology-style evidence dict (links: {code: [...], prs: [...]})
    # or any non-list value: skip the whole field rather than discarding the doc.
    # Only a proper list is iterated; malformed individual items are also skipped.
    if isinstance(raw_links, list):
        for item in raw_links:
            if not isinstance(item, dict):
                continue  # skip malformed item, keep parsing the rest
            kind = str(item.get("kind") or "").strip()
            if kind not in FEATURE_LINK_KINDS:
                continue  # skip item with unknown/missing kind
            links.append(FeatureLinkRef(
                kind=kind,
                project=str(item.get("project") or "").strip(),
                feature=str(item.get("feature") or "").strip(),
            ))

    body = "\n".join(lines[close + 1:]).lstrip("\n")
    return FeatureDoc(slug=slug, title=title, state=state, links=tuple(links), body=body)


def serialize_feature_doc(doc: FeatureDoc) -> str:
    meta: dict = {"slug": doc.slug, "title": doc.title, "state": doc.state}
    if doc.links:
        meta["links"] = [
            {"kind": link.kind, "project": link.project, "feature": link.feature}
            for link in doc.links
        ]
    front = yaml.safe_dump(meta, sort_keys=False).strip()
    return f"---\n{front}\n---\n{doc.body}"
