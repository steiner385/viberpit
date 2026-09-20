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

The alias list is written by the rename endpoint, so it only covers renames made
*after* the column existed. ``backfill_former_slugs`` reconstructs the rest from
the audit log at startup — see its docstring.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

#: The audit record ``PATCH /api/pm/projects/{slug}`` writes. Named here because
#: this module reads it back (``backfill_former_slugs``): the writer and the
#: reader of a rename's only durable trace must not drift apart.
PROJECT_UPDATE_AUDIT_TYPE = "pm.project.update"


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


# -- repairing renames that predate the column --------------------------------


def renames_from_audit(details: Iterable[Any]) -> list[tuple[str, str]]:
    """The ``(old_slug, new_slug)`` pairs in ``pm.project.update`` audit details.

    *details* must be in chronological order; anything that is not a rename (a
    name change, a repo repoint, a priority nudge) or is not shaped like one is
    skipped. The column is JSON written by a past version of the endpoint, so
    every level is type-checked rather than trusted.
    """
    out: list[tuple[str, str]] = []
    for detail in details:
        if not isinstance(detail, Mapping):
            continue
        changes = detail.get("changes")
        change = changes.get("slug") if isinstance(changes, Mapping) else None
        if not isinstance(change, Mapping):
            continue
        old, new = change.get("from"), change.get("to")
        if isinstance(old, str) and isinstance(new, str):
            out.append((old, new))
    return out


def rename_history(renames: Sequence[tuple[str, str]]) -> dict[str, list[str]]:
    """Fold chronological renames into ``final slug -> former slugs, oldest first``.

    The audit record names slugs, never the project id, so a product is followed
    through the chain by the slug each rename lands on: ``a→b`` then ``b→c``
    leaves ``{"c": ["a", "b"]}``. That is also what makes an adopted slug safe —
    ``foo→bar`` followed by ``baz→foo`` starts a *new* chain at ``foo`` rather
    than extending the old one, because the first chain has already moved on to
    ``bar``.
    """
    history: dict[str, list[str]] = {}
    for old, new in renames:
        history[new] = former_slugs_after_rename(
            history.pop(old, []), old_slug=old, new_slug=new)
    return {slug: olds for slug, olds in history.items() if olds}


async def backfill_former_slugs(db: Any) -> dict[str, list[str]]:
    """Reconstruct aliases for renames made before ``former_slugs`` existed.

    The rename endpoint records the old slug from now on, but the rename that
    filed #1531–#1537 (and their re-files #1875–#1881) — and the
    ``lakotagrades``→``fridgesheet`` rename made six hours before the alias
    column shipped — left no trace anywhere except the audit log. So the
    protection was prospective only: the very renames that motivated it stayed
    unprotected, and the next feature doc to name one of those old slugs would
    lose its edge exactly as before.

    Idempotent and cheap: one filtered read of an audit type with a handful of
    rows, and an UPDATE only for a product whose stored list actually changes.
    Returns the repaired ``slug -> former_slugs`` for the caller to log.
    """
    from sqlalchemy import select

    from server.db.models import AuditEvent, Project

    details = (await db.execute(
        select(AuditEvent.detail)
        .where(AuditEvent.type == PROJECT_UPDATE_AUDIT_TYPE)
        .order_by(AuditEvent.at, AuditEvent.id))).scalars().all()
    history = rename_history(renames_from_audit(details))
    if not history:
        return {}
    rows = (await db.execute(
        select(Project).where(Project.slug.in_(list(history))))).scalars().all()
    repaired: dict[str, list[str]] = {}
    for p in rows:
        live = str(p.slug)
        # Sanitize what is stored, then fold the audit-derived slugs in AHEAD of
        # it: anything the endpoint recorded is by definition the more recent
        # half of the history, and the list is documented oldest-first.
        stored = former_slugs_after_rename(p.former_slugs, old_slug=None, new_slug=live)
        merged: list[str] = []
        for old in [*history[live], *stored]:
            merged = former_slugs_after_rename(merged, old_slug=old, new_slug=live)
        if merged != stored:
            p.former_slugs = merged or None
            repaired[live] = merged
    return repaired
