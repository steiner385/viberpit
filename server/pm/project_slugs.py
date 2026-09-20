"""Resolving a product by slug across a rename (#1531–#1537/#1881).

``Project.slug`` is the product's identity everywhere *inside* this system, and
every foreign key is on ``Project.id``, so a rename is FK-safe. But one class of
reference lives outside the database and names the product by slug: a feature
doc's ``links:`` block, written by hand in whatever repo hosts the doc —

    links:
      - kind: powered_by
        project: viberpit          # ← a slug, in someone else's repo
        feature: config-dir-isolation

and ``_pm_sync_apply_feature_plan`` **rewrites every outgoing edge from the docs
on each apply**. So renaming a product used to delete each inbound cross-product
edge and fail to recreate it, silently, until a human edited every referring
doc. That is what filed #1531–#1537 (520 warnings) and their re-files #1875–#1881.

This module is the one place that maps doc-side slugs to project ids. It keeps a
product's previous slugs as aliases, the way GitHub keeps a redirect for a
renamed repository: the stale reference resolves, and the doc can be corrected
whenever its repo is next touched rather than as a rename-day emergency.

Aliases never win over a live slug (see ``resolve_project_slugs``), so adopting a
freed-up slug for a new product stays unambiguous.
"""
from __future__ import annotations

from typing import Any, Iterable, Sequence


def former_slugs_after_rename(current: Sequence[str] | None,
                              *, old_slug: str | None, new_slug: str) -> list[str]:
    """The alias list for a product renamed ``old_slug`` → ``new_slug``.

    Oldest first, so the list reads as the product's history. ``new_slug`` is
    dropped if present: a slug the product is being renamed *to* is live, not
    former, and leaving it in both places would make it its own alias.

    Deliberately uncapped. Trimming the oldest entry would re-break exactly the
    long-untouched doc this exists to protect, and a rename is a rare human
    action behind a ``pm.project.update`` audit record.
    """
    out = [s for s in (current or []) if isinstance(s, str) and s]
    if old_slug and old_slug != new_slug:
        out.append(old_slug)
    # dict.fromkeys de-dupes while keeping first-seen order (A→B→A→B renames).
    return [s for s in dict.fromkeys(out) if s != new_slug]


def _alias_map(projects: Iterable[Any]) -> dict[str, str]:
    """slug → project id for every *former* slug of *projects*.

    A former slug shared by two products (A was renamed away from it, B later
    was too) is ambiguous with no principled winner, so it resolves to neither.
    """
    seen: dict[str, str | None] = {}
    for p in projects:
        for alias in (p.former_slugs or []):
            if not isinstance(alias, str) or not alias:
                continue
            seen[alias] = None if alias in seen and seen[alias] != p.id else p.id
    return {slug: pid for slug, pid in seen.items() if pid is not None}


async def resolve_project_slugs(db: Any, slugs: Iterable[str]) -> dict[str, str]:
    """Map each requested slug to a project id, tolerating renames.

    A live ``Project.slug`` always wins over an alias, so re-adopting a freed-up
    slug for a new product points new docs at the new product while old docs
    that were never updated still reach the old one. Unresolvable slugs are
    simply absent from the result — the caller decides whether that is news.
    """
    from sqlalchemy import or_, select

    from server.db.models import Project

    wanted = [s for s in dict.fromkeys(slugs) if s]
    if not wanted:
        return {}
    # One query: rows matching by live slug, plus any row carrying one of these
    # slugs as an alias. JSON has no portable "contains" across SQLite and
    # Postgres, so alias candidates are narrowed in Python from the rows that
    # have any alias at all.
    rows = (await db.execute(select(Project).where(or_(
        Project.slug.in_(wanted), Project.former_slugs.is_not(None))))).scalars().all()
    resolved = {str(p.slug): str(p.id) for p in rows if p.slug in wanted}
    for slug, pid in _alias_map(rows).items():
        if slug in wanted and slug not in resolved:
            resolved[slug] = str(pid)
    return resolved
