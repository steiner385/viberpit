"""Slug generation — single source of truth (constitution VI).

Moved out of ``server/api/sdlc.py`` (was ``_slugify``, private to that module)
so ``mirror_store.adopt_epics`` (spec 003 Phase C) can share it instead of
re-inlining the same regex (issue #991)."""
from __future__ import annotations

import re


def slugify(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return s or "initiative"
