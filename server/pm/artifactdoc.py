"""SDLC artifact frontmatter convention: parse ``docs/sdlc/<slug>/<kind>.md``.

Sibling to ``featuredoc.py`` and built on the same principle (spec issue
#2161): the file, not the DB, is the authority for the planning relationships
the design (§2) calls "git is the master audit trail". YAML frontmatter
carries ``initiative``/``kind``/``epic``/``capabilities``/``upstream``/
``verified_against``; the markdown body is the artifact itself. Pure, no I/O —
pm-sync feeds committed file text in and reconciles ``InitiativeArtifact`` /
``Initiative`` / ``InitiativeFeature`` from the result (see
``server/pm/artifact_sync_seams.py``).

Same reading posture as ``featuredoc.py``: a doc with NO frontmatter fence at
all is not an error — the caller already knows ``initiative``/``kind`` from
the ``docs/sdlc/<slug>/<kind>.md`` path itself, so a pre-convention doc is
still identifiable, just flagged by the caller as having no frontmatter
(``frontmatter_present=False`` on ``InitiativeArtifact``) rather than dropped.
What still fails loudly: a fence that never closes, or YAML that doesn't
parse — no convention rescues those.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import yaml

from server.db.models import INITIATIVE_ARTIFACT_KINDS

#: "owner/repo#123" — the same shape used everywhere else in the codebase for
#: an epic/issue pointer (``server/automations/backlog_github.py``,
#: ``Initiative.epic_repo``/``epic_number``).
_EPIC_RE = re.compile(r"^[\w.-]+/[\w.-]+#\d+$")


class ArtifactDocError(ValueError):
    """Raised for a malformed artifact doc. Callers log and skip — one bad
    doc must never poison the frontmatter sync pass, same as ``featuredoc``."""


@dataclass(frozen=True)
class ArtifactDoc:
    initiative: str
    kind: str
    epic: str | None = None
    capabilities: tuple[str, ...] = ()
    upstream: tuple[str, ...] = ()
    verified_against: str | None = None
    body: str = ""


def _str_list(raw: object) -> tuple[str, ...]:
    """A list of non-empty scalars from *raw*: a real list, or a lone string
    promoted to a one-item list. Anything else (a dict, None) yields none."""
    items = raw if isinstance(raw, list) else [raw] if isinstance(raw, str) else []
    out: list[str] = []
    for item in items:
        if isinstance(item, (dict, list)) or item is None:
            continue
        s = str(item).strip()
        if s:
            out.append(s)
    return tuple(out)


def parse_artifact_doc(text: str, *, slug: str | None = None, kind: str | None = None) -> ArtifactDoc:
    """Parse one SDLC artifact doc. *slug*/*kind* are what the caller already
    knows from the ``docs/sdlc/<slug>/<kind>.md`` path — they fill in an
    absent ``initiative:``/``kind:`` key, mirroring how ``parse_feature_doc``'s
    *path* fills in a missing ``slug``."""
    meta: dict = {}
    if text.startswith("---"):
        lines = text.splitlines()
        try:
            close = next(i for i, ln in enumerate(lines[1:], start=1) if ln.strip() == "---")
        except StopIteration:
            raise ArtifactDocError("unterminated frontmatter fence") from None
        raw = "\n".join(lines[1:close])
        try:
            loaded = yaml.safe_load(raw) or {}
        except yaml.YAMLError as e:
            raise ArtifactDocError(f"bad yaml frontmatter: {e}") from e
        if not isinstance(loaded, dict):
            raise ArtifactDocError("frontmatter is not a mapping")
        meta = loaded
        body = "\n".join(lines[close + 1:]).lstrip("\n")
    else:
        # No fence at all — a pre-convention doc. Not an error: the path
        # already names the initiative and kind unambiguously, so the caller
        # can still index it (and separately record that it has no frontmatter).
        body = text.lstrip("\n")

    initiative = str(meta.get("initiative") or "").strip() or (slug or "")
    doc_kind = str(meta.get("kind") or "").strip() or (kind or "")
    if not initiative or not doc_kind:
        raise ArtifactDocError("initiative and kind are required")
    if doc_kind not in INITIATIVE_ARTIFACT_KINDS:
        raise ArtifactDocError(f"kind {doc_kind!r} not in {INITIATIVE_ARTIFACT_KINDS}")

    epic_raw = meta.get("epic")
    epic = str(epic_raw).strip() if epic_raw is not None else ""
    if epic and not _EPIC_RE.match(epic):
        raise ArtifactDocError(f"epic {epic!r} is not owner/repo#N")

    verified_raw = meta.get("verified_against")
    verified_against = str(verified_raw).strip() if verified_raw is not None else ""

    return ArtifactDoc(
        initiative=initiative,
        kind=doc_kind,
        epic=epic or None,
        capabilities=_str_list(meta.get("capabilities")),
        upstream=_str_list(meta.get("upstream")),
        verified_against=verified_against or None,
        body=body,
    )
