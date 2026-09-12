"""Slug generation — single source of truth (constitution VI).

Moved out of ``server/api/sdlc.py`` (was ``_slugify``, private to that module)
so ``mirror_store.adopt_epics`` (spec 003 Phase C) can share it instead of
re-inlining the same regex (issue #991)."""
from __future__ import annotations

import re


def slugify(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return s or "initiative"


_EPIC_PREFIX = re.compile(r"^\s*(?:epic|program)\s*(?:\([^)]*\))?\s*:\s*", re.IGNORECASE)
_WS = re.compile(r"\s+")


def normalize_epic_title(title: str) -> str:
    """An initiative title from a GitHub epic title: the conventional-commit
    ``epic(scope):`` prefix is a GitHub filing convention, not a name, and
    slugifying it (``epic-ui-audit-04-…``) leaked it into every initiative slug.
    Whitespace is collapsed. An empty remainder keeps the original (stripped)
    so a title can never become blank."""
    stripped = _WS.sub(" ", title).strip()
    out = _WS.sub(" ", _EPIC_PREFIX.sub("", stripped, count=1)).strip()
    return out or stripped
