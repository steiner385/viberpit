"""Feature-doc convention: parse/serialize ``docs/product/features/<slug>.md``.

YAML frontmatter carries the machine-readable index fields; the markdown body
is the human artifact (spec §3). Pure functions, no I/O — the sync loop feeds
file text in and upserts Feature rows from the result.

Canonical frontmatter is ``slug``/``title``/``state`` — what
:func:`serialize_feature_doc` writes. Reading is deliberately wider than
writing: ``capability`` is accepted for ``slug``, ``status`` for ``state``, and
a missing ``title`` falls back to the body's first H1. Docs written by an agent
from a prose template land in that dialect, and dropping them is worse than
indexing them, because the skip is silent and an unindexed capability cannot
latch the pm-triage drift branch (see ``server/pm/engine.py``).
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
    parent: str | None = None
    labels: tuple[str, ...] = ()


def _title_from_body(body: str) -> str:
    """The first markdown H1 in *body*, or ``""``.

    Only ``# `` at the start of a line counts, and the search stops at the
    first one — a later ``#`` inside a fenced block cannot supply a title.
    """
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


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

    body = "\n".join(lines[close + 1:]).lstrip("\n")

    # Accept the `capability:`/`status:` spelling and an H1-only title.
    # Agent-authored docs converge on that shape because the prompt that asks
    # for a feature doc describes the BODY sections and never the frontmatter,
    # so the writer invents keys. Rejecting them is technically correct and
    # operationally terrible: the doc is unambiguous, the parse failure is
    # silent, and an unindexed capability can never latch the triage loop's
    # drift branch — which is how one repo ran fourteen docs, zero indexed
    # features, and ~40 duplicate "propose this capability" sessions.
    slug = str(meta.get("slug") or meta.get("capability") or "").strip()
    title = str(meta.get("title") or "").strip() or _title_from_body(body)
    state = str(meta.get("state") or meta.get("status") or "").strip()
    if not slug or not title:
        raise FeatureDocError("slug and title are required")
    if state not in FEATURE_LIFECYCLE_STATES:
        raise FeatureDocError(f"state {state!r} not in {FEATURE_LIFECYCLE_STATES}")

    parent_raw = meta.get("parent")
    parent = str(parent_raw).strip() if parent_raw is not None else ""
    if parent == slug:
        raise FeatureDocError("parent cannot be the doc itself")

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

    raw_labels = meta.get("labels")
    labels = tuple(str(x).strip() for x in raw_labels if str(x).strip()) if isinstance(raw_labels, list) else ()

    return FeatureDoc(
        slug=slug, title=title, state=state, links=tuple(links), body=body, parent=parent or None,
        labels=labels,
    )


def serialize_feature_doc(doc: FeatureDoc) -> str:
    meta: dict = {"slug": doc.slug, "title": doc.title, "state": doc.state}
    if doc.parent:
        meta["parent"] = doc.parent
    if doc.labels:
        meta["labels"] = list(doc.labels)
    if doc.links:
        meta["links"] = [
            {"kind": link.kind, "project": link.project, "feature": link.feature}
            for link in doc.links
        ]
    front = yaml.safe_dump(meta, sort_keys=False).strip()
    return f"---\n{front}\n---\n{doc.body}"
