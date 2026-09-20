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

The rule that decides how wide to read is **infer what is unstated, never
override what is stated**:

* *Unstated* has an unambiguous reading, so it is filled in. No ``slug`` and no
  fence at all? ``docs/product/features/<slug>.md`` *is* the slug convention, so
  the caller's ``path`` supplies it (#1874, #1635). No ``state``? ``proposed``
  is the least-committal lifecycle value and cannot over-claim.
* *Stated but non-canonical* is honoured only where one canonical value is
  unambiguous — ``in-progress`` means ``delivering`` (#1903). Everything else
  stays an error: guessing at ``state: purple`` would silently contradict the
  author, which is worse than a doc the operator is told about.

Every path here still fails loudly for text no convention can rescue: an
unterminated fence, broken YAML, or no derivable slug/title.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import yaml

from server.db.models import FEATURE_LIFECYCLE_STATES, FEATURE_LINK_KINDS

#: Branch-safe capability slug. Lives here because it is part of the doc
#: convention — ``map_changes_ops`` imports it rather than defining a second one.
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

#: Lifecycle words that agent-authored docs actually use, mapped to the single
#: canonical state each one unambiguously means. Keys are normalised by
#: :func:`_normalise_state` (lowercased, ``_``/spaces folded to ``-``), so one
#: entry covers ``in_progress``/``In Progress``/``IN-PROGRESS``.
#:
#: Only add a word whose canonical meaning is not in doubt. A near-miss belongs
#: in the error, not in this table: the point is to index docs whose author was
#: clear, not to guess at docs whose author was not. ``active`` is the worked
#: example of what stays out — it reads as "actively being built" and "actively
#: in use" equally well, so it names two states and therefore none.
FEATURE_STATE_ALIASES: dict[str, str] = {
    # proposed
    "planned": "proposed", "proposal": "proposed", "draft": "proposed",
    "idea": "proposed", "backlog": "proposed", "todo": "proposed",
    "not-started": "proposed",
    # discovery
    "exploring": "discovery", "research": "discovery", "researching": "discovery",
    "investigating": "discovery", "spike": "discovery",
    # delivering
    "in-progress": "delivering", "inprogress": "delivering", "wip": "delivering",
    "building": "delivering", "implementing": "delivering",
    "in-development": "delivering", "development": "delivering",
    # live
    "done": "live", "complete": "live", "completed": "live", "shipped": "live",
    "released": "live", "ga": "live", "stable": "live",
    # sunset
    "deprecated": "sunset", "retired": "sunset", "sunsetting": "sunset",
    "archived": "sunset", "eol": "sunset",
}


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


def _normalise_state(raw: str) -> str:
    """The canonical lifecycle state *raw* names, or ``""`` if it names none.

    Case, underscores and spaces are spelling, not meaning, so they are folded
    before both the canonical check and the alias lookup.
    """
    key = re.sub(r"[\s_]+", "-", raw.strip().lower())
    if key in FEATURE_LIFECYCLE_STATES:
        return key
    return FEATURE_STATE_ALIASES.get(key, "")


def _slug_from_path(path: str | None) -> str:
    """The capability slug ``docs/product/features/<slug>.md`` encodes.

    ``""`` for no path or a stem that is not a valid slug — a stray ``README.md``
    in the directory must not become a capability.
    """
    if not path:
        return ""
    stem = path.rsplit("/", 1)[-1].removesuffix(".md").strip()
    return stem if SLUG_RE.match(stem) else ""


def parse_feature_doc(text: str, *, path: str | None = None) -> FeatureDoc:
    """Parse one feature doc. *path* is its repo path when the caller knows it,
    which is what lets a doc with no frontmatter still be indexed (see module
    docstring)."""
    meta: dict = {}
    if text.startswith("---"):
        lines = text.splitlines()
        try:
            close = next(i for i, ln in enumerate(lines[1:], start=1) if ln.strip() == "---")
        except StopIteration:
            raise FeatureDocError("unterminated frontmatter fence") from None
        raw = "\n".join(lines[1:close])
        try:
            loaded = yaml.safe_load(raw) or {}
        except yaml.YAMLError as e:
            raise FeatureDocError(f"bad yaml frontmatter: {e}") from e
        if not isinstance(loaded, dict):
            raise FeatureDocError("frontmatter is not a mapping")
        meta = loaded
        body = "\n".join(lines[close + 1:]).lstrip("\n")
    elif _slug_from_path(path):
        # No fence at all — the whole file is body, and the path names the
        # capability. Docs predating the frontmatter convention land here
        # (#1874, #1635); without this they are dropped on every sync pass in
        # repos that have no CI guard over their own docs.
        body = text.lstrip("\n")
    else:
        raise FeatureDocError("missing frontmatter fence")

    # Accept the `capability:`/`status:` spelling and an H1-only title.
    # Agent-authored docs converge on that shape because the prompt that asks
    # for a feature doc describes the BODY sections and never the frontmatter,
    # so the writer invents keys. Rejecting them is technically correct and
    # operationally terrible: the doc is unambiguous, the parse failure is
    # silent, and an unindexed capability can never latch the triage loop's
    # drift branch — which is how one repo ran fourteen docs, zero indexed
    # features, and ~40 duplicate "propose this capability" sessions.
    slug = str(meta.get("slug") or meta.get("capability") or "").strip() or _slug_from_path(path)
    title = str(meta.get("title") or "").strip() or _title_from_body(body)
    raw_state = str(meta.get("state") or meta.get("status") or "").strip()
    if not slug or not title:
        raise FeatureDocError("slug and title are required")
    # Unstated is the least-committal state, not a failure; stated-but-unknown
    # is still an error we must not guess our way past (module docstring).
    state = _normalise_state(raw_state) if raw_state else "proposed"
    if not state:
        raise FeatureDocError(f"state {raw_state!r} not in {FEATURE_LIFECYCLE_STATES}")

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
