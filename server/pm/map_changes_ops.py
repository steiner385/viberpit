"""Pure map-changes edits (roadmap drill-down spec §8, §9).

One capability edit → one new Markdown text. The doc's frontmatter is the truth
for slug/title/state/parent/links; its body is the human prose. Reads through
``parse_feature_doc`` (which tolerates the archaeology dialect) and writes
through ``serialize_feature_doc`` (canonical), so the first machine edit of an
old doc canonicalises it — the PR body says so (R8b-12). No I/O here: the
service layer fetches the current text and commits the result."""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from server.db.models import FEATURE_LINK_KINDS

from .classification import feature_doc_path
from .featuredoc import (
    FeatureDoc,
    FeatureDocError,
    FeatureLinkRef,
    parse_feature_doc,
    serialize_feature_doc,
)

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MAP_OPS: tuple[str, ...] = ("create", "rename", "reparent", "summary", "links")


class MapOpError(ValueError):
    """A rejected edit. ``code`` is the API's ``detail.error``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class MapOp:
    op: str
    slug: str
    title: str | None = None
    parent_slug: str | None = None
    summary: str | None = None
    links: tuple[FeatureLinkRef, ...] = ()


@dataclass(frozen=True)
class MapContext:
    """The product's capability slugs → parent slug (indexed rows merged with
    the branch's docs) and every (project slug, feature slug) a link may target."""
    parents: Mapping[str, str | None]
    link_targets: frozenset[tuple[str, str]]


def _is_ancestor(ctx: MapContext, candidate: str, of: str) -> bool:
    """True when ``candidate`` is ``of`` itself or one of its descendants —
    i.e. making it ``of``'s parent would close a cycle."""
    seen: set[str] = set()
    cur: str | None = candidate
    while cur is not None and cur not in seen:
        if cur == of:
            return True
        seen.add(cur)
        cur = ctx.parents.get(cur)
    return False


def validate_op(op: MapOp, ctx: MapContext) -> None:
    if op.op not in MAP_OPS:
        raise MapOpError("bad_op", f"op must be one of {MAP_OPS}")
    if not SLUG_RE.match(op.slug or ""):
        raise MapOpError("bad_slug", "slug must be lowercase letters, digits and dashes")
    exists = op.slug in ctx.parents
    if op.op == "create":
        if exists:
            raise MapOpError("slug_taken", f"capability '{op.slug}' already exists")
    elif not exists:
        raise MapOpError("not_found", f"capability '{op.slug}' does not exist")
    if op.op in ("create", "rename"):
        if not (op.title or "").strip():
            raise MapOpError("bad_title", "title must not be blank")
    if op.op in ("create", "reparent") and op.parent_slug is not None:
        if op.parent_slug not in ctx.parents:
            raise MapOpError("parent_not_found", f"parent '{op.parent_slug}' does not exist")
        if op.op == "reparent" and _is_ancestor(ctx, op.parent_slug, op.slug):
            raise MapOpError("parent_cycle", "a capability cannot sit under itself or its descendants")
    if op.op == "summary" and not (op.summary or "").strip():
        raise MapOpError("bad_summary", "summary must not be blank")
    if op.op in ("links", "create"):
        for link in op.links:
            if link.kind not in FEATURE_LINK_KINDS:
                raise MapOpError("bad_link", f"link kind must be one of {FEATURE_LINK_KINDS}")
            if (link.project, link.feature) not in ctx.link_targets:
                raise MapOpError("bad_link", f"unknown link target {link.project}/{link.feature}")


def _replace_summary(body: str, summary: str) -> str:
    """Replace the first non-heading LINE — exactly what the index reads as the
    summary (``feature_index._summary``) — and nothing after it; insert one
    after the leading H1 when the body has no prose. Replacing the whole
    paragraph deleted every wrapped line of a multi-line first paragraph, which
    most real docs have (8b Task 2 review)."""
    lines = body.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip() and not line.lstrip().startswith("#"):
            start = i
            break
    if start is None:
        head = lines[:1] if lines and lines[0].startswith("# ") else []
        rest = lines[len(head):]
        return "\n".join(
            [*head, "", summary.strip(), *(([""] if rest[0].strip() else []) if rest else []), *rest]
        ).rstrip("\n") + "\n"
    return "\n".join([*lines[:start], summary.strip(), *lines[start + 1:]]).rstrip("\n") + "\n"


def apply_op(op: MapOp, current: str | None) -> tuple[str, str]:
    """The doc path and its new text. ``current`` is the text at the branch
    head (``None`` only for ``create``). Validation is ``validate_op``'s job;
    this only refuses the shapes it cannot compute."""
    path = feature_doc_path(op.slug)
    if op.op == "create":
        title = (op.title or "").strip()
        summary = (op.summary or "").strip()
        body = f"# {title}\n" + (f"\n{summary}\n" if summary else "")
        doc = FeatureDoc(slug=op.slug, title=title, state="proposed",
                         links=tuple(op.links), body=body, parent=op.parent_slug)
        return path, serialize_feature_doc(doc)
    if current is None:
        raise MapOpError("not_found", f"no doc for '{op.slug}' on the branch")
    try:
        doc = parse_feature_doc(current)
    except FeatureDocError as e:
        raise MapOpError("bad_doc", f"the existing doc cannot be parsed: {e}") from e
    if op.op == "rename":
        doc = FeatureDoc(slug=doc.slug, title=(op.title or "").strip(), state=doc.state,
                         links=doc.links, body=doc.body, parent=doc.parent)
    elif op.op == "reparent":
        doc = FeatureDoc(slug=doc.slug, title=doc.title, state=doc.state,
                         links=doc.links, body=doc.body, parent=op.parent_slug)
    elif op.op == "summary":
        doc = FeatureDoc(slug=doc.slug, title=doc.title, state=doc.state, links=doc.links,
                         body=_replace_summary(doc.body, op.summary or ""), parent=doc.parent)
    elif op.op == "links":
        doc = FeatureDoc(slug=doc.slug, title=doc.title, state=doc.state,
                         links=tuple(op.links), body=doc.body, parent=doc.parent)
    else:
        raise MapOpError("bad_op", f"op must be one of {MAP_OPS}")
    return path, serialize_feature_doc(doc)


def commit_message(op: MapOp, product_name: str) -> str:
    verb = {"create": "add capability", "rename": "rename capability",
            "reparent": "move capability", "summary": "update summary of",
            "links": "update links of"}[op.op]
    slug_prefix = product_name.lower().replace(" ", "-")
    return f"map({slug_prefix}): {verb} {op.slug}"
