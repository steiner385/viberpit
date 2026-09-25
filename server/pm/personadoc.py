"""Persona-doc convention: parse ``docs/product/personas/<slug>.md`` (issue
#2164). Pure, no I/O — mirrors ``server/pm/featuredoc.py``'s reading rules
(frontmatter, or a bare body whose path supplies the slug) but carries none of
that module's evidence/links/map-changes-branch machinery: a persona is an
identity doc, not a code capability, so pm-sync indexes it default-branch-only.
"""
from __future__ import annotations

from dataclasses import dataclass

import yaml

from server.pm.featuredoc import SLUG_RE


class PersonaDocError(ValueError):
    """Raised for a malformed persona doc. Callers log and skip — one bad doc
    must never poison the whole index (mirrors FeatureDocError)."""


@dataclass(frozen=True)
class PersonaDoc:
    slug: str
    title: str
    role: str | None
    age_band: str | None
    tier: str | None
    body: str


def _title_from_body(body: str) -> str:
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def _slug_from_path(path: str | None) -> str:
    if not path:
        return ""
    stem = path.rsplit("/", 1)[-1].removesuffix(".md").strip()
    return stem if SLUG_RE.match(stem) else ""


def parse_persona_doc(text: str, *, path: str | None = None) -> PersonaDoc:
    meta: dict = {}
    has_frontmatter = False
    if text.startswith("---"):
        has_frontmatter = True
        lines = text.split("\n")
        try:
            close = next(i for i, ln in enumerate(lines[1:], start=1) if ln.strip() == "---")
        except StopIteration:
            raise PersonaDocError("unterminated frontmatter fence") from None
        raw = "\n".join(lines[1:close])
        try:
            loaded = yaml.safe_load(raw) or {}
        except yaml.YAMLError as e:
            raise PersonaDocError(f"bad yaml frontmatter: {e}") from e
        if not isinstance(loaded, dict):
            raise PersonaDocError("frontmatter is not a mapping")
        meta = loaded
        body = "\n".join(lines[close + 1:]).lstrip("\n")
    elif _slug_from_path(path):
        body = text.lstrip("\n")
    else:
        raise PersonaDocError("missing frontmatter fence")

    slug = str(meta.get("slug") or "").strip() or _slug_from_path(path)
    title = str(meta.get("title") or "").strip() or _title_from_body(body)

    # When frontmatter is present, both slug and title are required (mirrors featuredoc.py).
    # When there's no frontmatter (fenceless/path-derived-slug), only slug is required,
    # since title is optional metadata and can be empty.
    if has_frontmatter:
        if not slug or not title:
            raise PersonaDocError("slug and title are required")
    else:
        if not slug:
            raise PersonaDocError("slug is required")

    def _opt(key: str) -> str | None:
        v = meta.get(key)
        v = str(v).strip() if v is not None else ""
        return v or None

    return PersonaDoc(slug=slug, title=title, role=_opt("role"),
                      age_band=_opt("age_band"), tier=_opt("tier"), body=body)
