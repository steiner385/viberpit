"""Product repo lifecycle: add, set primary, retire, un-retire.

Invariants (docs/superpowers/specs/2026-11-18-product-repo-lifecycle-design.md § Data):
exactly one active primary per product, a retired repo is never primary, and at least
one repo stays active. Nothing is deleted — "detach" means retire.

Callers (``server/api/pm.py``) own the transaction: they take
``lock_repo_registration`` (after any ownership preflight) before calling in, and the
request session commits. Every refusal is a :class:`RepoLifecycleError` (or
``RepoRetired`` / ``OwnershipConflict``) raised before any write, so a refused call
changes nothing.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.audit import record_audit
from server.db.clock import db_utc_now
from server.db.models import Project, ProjectRepo
from server.pm.map_changes import open_pointer_row
from server.pm.product_repos import REPO_ORDER, RepoRetired, invalidate_retired_cache
from server.pm.repo_project import RepoOwnershipSnapshot, require_unregistered_repos

AUDIT_ADD = "pm.project.repo.add"
AUDIT_PRIMARY = "pm.project.repo.primary"
AUDIT_RETIRE = "pm.project.repo.retire"
AUDIT_UNRETIRE = "pm.project.repo.unretire"


class RepoLifecycleError(ValueError):
    """A refused lifecycle change; ``code``/``status`` are the HTTP error mapping."""

    def __init__(self, code: str, message: str, *, status: int):
        self.code = code
        self.status = status
        super().__init__(message)


async def _rows(db: AsyncSession, project: Project) -> list[ProjectRepo]:
    return list((await db.execute(
        select(ProjectRepo).where(ProjectRepo.project_id == project.id).order_by(*REPO_ORDER)
    )).scalars().all())


def _match(rows: list[ProjectRepo], repo: str | None) -> ProjectRepo | None:
    key = (repo or "").casefold()
    return next((r for r in rows if (r.repo or "").casefold() == key), None) if key else None


def _active_primary(rows: list[ProjectRepo]) -> ProjectRepo | None:
    return next((r for r in rows if r.is_primary and r.retired_at is None), None)


def _require(rows: list[ProjectRepo], repo: str, project: Project) -> ProjectRepo:
    row = _match(rows, repo)
    if row is None:
        raise RepoLifecycleError(
            "not_found", f"Repo {repo} is not attached to product '{project.slug}'", status=404)
    return row


def _check_expected(rows: list[ProjectRepo], expected_primary: str | None) -> None:
    if expected_primary is None:
        return
    current = _active_primary(rows)
    if ((current.repo if current is not None else None) or "").casefold() \
            != expected_primary.casefold():
        raise RepoLifecycleError(
            "primary_changed",
            f"The primary repo is now {current.repo if current else 'unset'}, "
            f"not {expected_primary}; reload and retry.", status=409)


async def _refuse_if_map_changes_open(db: AsyncSession, project: Project) -> None:
    # The map-changes service writes to the primary repo it derives at use time, so
    # moving the primary under an open branch/PR would orphan it.
    if await open_pointer_row(db, project.id) is not None:
        raise RepoLifecycleError(
            "open_map_changes",
            "Merge or abandon the open map-changes PR before changing the primary repo.",
            status=409)


def _make_primary(rows: list[ProjectRepo], row: ProjectRepo) -> None:
    for other in rows:
        if other is not row and other.is_primary:
            other.is_primary = False
    row.is_primary = True


async def _now(db: AsyncSession):
    return (await db.execute(select(db_utc_now()))).scalar_one()


async def _written(db: AsyncSession, project: Project, type_: str, identity: str,
                   detail: dict) -> None:
    await db.flush()
    await record_audit(db=db, type=type_, identity=identity,
                       detail={"slug": project.slug, **detail})
    invalidate_retired_cache()


async def add_repo(db: AsyncSession, project: Project, repo: str, *, make_primary: bool,
                   snapshot: RepoOwnershipSnapshot, identity: str) -> ProjectRepo:
    """Attach ``repo`` as an active repo (the primary when ``make_primary`` or when the
    product has none). A repo already attached to THIS product is reused — un-retired if
    retired — never duplicated. Raises ``OwnershipConflict`` when another product owns it.
    """
    rows = await _rows(db, project)
    current = _active_primary(rows)
    row = _match(rows, repo)
    if row is not None:
        if row.retired_at is not None:
            await unretire_repo(db, project, row.repo or repo, identity=identity)
        if make_primary:
            await set_primary(db, project, row.repo or repo, identity=identity)
        return row
    # Registration evidence must be re-checked before any write in this request
    # (its SELECT autoflushes pending writes — see update_project).
    canonical = (await require_unregistered_repos(db, [repo], snapshot=snapshot))[repo]
    primary = make_primary or current is None
    if primary and current is not None:
        await _refuse_if_map_changes_open(db, project)
    row = ProjectRepo(project_id=project.id, repo=canonical, is_primary=False)
    db.add(row)
    if primary:
        _make_primary(rows, row)
    await _written(db, project, AUDIT_ADD, identity, {
        "repo": canonical, "primary": primary,
        "previousPrimary": current.repo if current is not None and primary else None})
    return row


async def set_primary(db: AsyncSession, project: Project, repo: str, *, identity: str,
                      expected_primary: str | None = None) -> None:
    rows = await _rows(db, project)
    row = _require(rows, repo, project)
    _check_expected(rows, expected_primary)
    if row.retired_at is not None:
        raise RepoRetired(row.repo or repo)
    current = _active_primary(rows)
    if current is row:
        return
    await _refuse_if_map_changes_open(db, project)
    _make_primary(rows, row)
    await _written(db, project, AUDIT_PRIMARY, identity, {
        "from": current.repo if current is not None else None, "to": row.repo})


async def retire_repo(db: AsyncSession, project: Project, repo: str, *, identity: str,
                      reason: str | None = None, superseded_by: str | None = None,
                      new_primary: str | None = None,
                      expected_primary: str | None = None) -> None:
    """Retire ``repo``. Retiring the primary requires ``new_primary`` (an active repo of
    this product); otherwise ``new_primary`` may only restate the current primary.
    Retiring an already-retired repo is a no-op."""
    rows = await _rows(db, project)
    row = _require(rows, repo, project)
    _check_expected(rows, expected_primary)
    if row.retired_at is not None:
        return
    if not any(r.retired_at is None and r is not row for r in rows):
        raise RepoLifecycleError(
            "last_active_repo",
            f"{row.repo} is the last active repo of '{project.slug}'; add another first.",
            status=422)
    current = _active_primary(rows)
    successor: ProjectRepo | None = None
    if row is current:
        if not new_primary:
            raise RepoLifecycleError(
                "new_primary_required",
                f"{row.repo} is the primary repo; name the new primary to retire it.",
                status=422)
        successor = _match(rows, new_primary)
        if successor is None or successor is row or successor.retired_at is not None:
            raise RepoLifecycleError(
                "invalid_new_primary",
                f"newPrimary {new_primary} is not another active repo of '{project.slug}'.",
                status=422)
        await _refuse_if_map_changes_open(db, project)
    elif new_primary and _match(rows, new_primary) is not current:
        raise RepoLifecycleError(
            "invalid_new_primary",
            f"{row.repo} is not the primary; newPrimary must be omitted or the current "
            "primary.", status=422)
    row.retired_at = await _now(db)
    row.retired_reason = reason
    row.superseded_by = superseded_by
    row.is_primary = False
    if successor is not None:
        _make_primary(rows, successor)
    await _written(db, project, AUDIT_RETIRE, identity, {
        "repo": row.repo, "reason": reason, "supersededBy": superseded_by,
        "newPrimary": successor.repo if successor is not None else None})


async def unretire_repo(db: AsyncSession, project: Project, repo: str, *,
                        identity: str) -> None:
    """Make a retired repo active again (not primary). Already active: no-op."""
    rows = await _rows(db, project)
    row = _require(rows, repo, project)
    if row.retired_at is None:
        return
    row.retired_at = None
    row.retired_reason = None
    row.superseded_by = None
    await _written(db, project, AUDIT_UNRETIRE, identity, {"repo": row.repo})
