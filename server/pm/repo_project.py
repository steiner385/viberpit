"""One batched repo→product ownership resolver for PM and automation callers.

Identity snapshots are gathered before entering this DB boundary. Registration-only
compatibility reads cannot establish renamed aliases or verified absence.
"""
from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from server.db.models import Project, ProjectRepo
from server.pm.github_sync import canonical_repo_name


@dataclass(frozen=True)
class RepoOwner:
    """A product identified by both its stable id and usable slug."""

    project_id: str
    slug: str


@dataclass(frozen=True)
class RepoOwnership:
    requested_repo: str
    canonical_repo: str | None
    state: Literal["unique", "ambiguous", "missing", "unresolved"]
    owner: RepoOwner | None
    candidates: tuple[RepoOwner, ...]
    unresolved_repos: tuple[str, ...]
    reason: str


async def resolve_repo_ownership(
    db: AsyncSession,
    repos: Collection[str],
    *,
    canonical_repos: Mapping[str, str | None] | None = None,
) -> dict[str, RepoOwnership]:
    """Resolve a batch with one read and no network calls or data changes.

    ``canonical_repos`` is evaluation-scoped, verified GitHub nameWithOwner
    evidence, keyed by requested/registered spelling. Missing, invalid, failed,
    or contradictory entries are unknown. All registrations must be covered:
    an unknown alias could name any requested repository. Verified unrelated
    registrations do not interfere. Multiple aliases in one product coalesce.

    Without a snapshot, only case-insensitive registration matches are examined
    for legacy callers. A local unique match has no canonical identity evidence;
    unmatched names are unresolved, never verified missing. New authorization
    callers must supply a snapshot, including an empty one when lookup failed.
    """
    from server.automations.target_identity import valid_repo_name

    wanted = sorted({repo for repo in repos if repo})
    if not wanted:
        return {}
    rows = (await db.execute(
        select(ProjectRepo.repo, Project.id, Project.slug)
        .outerjoin(Project, Project.id == ProjectRepo.project_id)
        .where(ProjectRepo.repo.isnot(None))
    )).all()

    identities: dict[str, str | None] = {}
    if canonical_repos is not None:
        for alias, value in canonical_repos.items():
            canonical = canonical_repo_name({"data": {"repository": {"nameWithOwner": value}}})
            if not valid_repo_name(canonical):
                canonical = None
            key = alias.casefold()
            previous = identities.get(key)
            if key in identities and (
                previous is None or canonical is None
                or previous.casefold() != canonical.casefold()
            ):
                identities[key] = None
            else:
                identities[key] = canonical

    out: dict[str, RepoOwnership] = {}
    for requested in wanted:
        canonical = identities.get(requested.casefold())
        candidates: dict[str, RepoOwner] = {}
        unresolved: set[str] = set()
        if canonical_repos is not None and canonical is None:
            unresolved.add(requested)
        for registered, project_id, slug in rows:
            if not registered:
                continue
            if canonical_repos is None:
                matches = registered.casefold() == requested.casefold()
            else:
                registered_canonical = identities.get(registered.casefold())
                if registered_canonical is None:
                    unresolved.add(registered)
                    continue
                matches = canonical is not None and registered_canonical.casefold() == canonical.casefold()
            if not matches:
                continue
            if not project_id or not slug:
                unresolved.add(registered)
            else:
                candidates[project_id] = RepoOwner(project_id, slug)
        owners = tuple(sorted(candidates.values(), key=lambda owner: (owner.slug, owner.project_id)))
        owner = None
        state: Literal["unique", "ambiguous", "missing", "unresolved"]
        if unresolved:
            state, reason = "unresolved", "Repository identity or product registration is unresolved."
        elif len(owners) > 1:
            state, reason = "ambiguous", "Repository is registered to multiple products."
        elif len(owners) == 1:
            state, reason = "unique", "Repository has one registered product."
            owner = owners[0]
        elif canonical_repos is not None:
            state, reason = "missing", "Verified repository has no registered product."
        else:
            state, reason = "unresolved", "No matching registration; repository identity is unverified."
            unresolved.add(requested)
        out[requested] = RepoOwnership(
            requested, canonical, state, owner, owners, tuple(sorted(unresolved)), reason,
        )
    return out


async def primary_repo_for_slug(db: AsyncSession, slug: str) -> str | None:
    """A product's primary repo (``None`` for an unknown slug or one with no primary):
    what a product-scoped notification carries as its ``repo``, and what a
    product-scoped watch matches on."""
    return (await db.execute(
        select(ProjectRepo.repo).join(Project, Project.id == ProjectRepo.project_id)
        .where(Project.slug == slug, ProjectRepo.is_primary.is_(True)))).scalars().first()


async def owning_projects_by_repo(
    db: AsyncSession, repos: Collection[str],
) -> dict[str, RepoOwner]:
    """Compatibility projection of unique local registrations only.

    Ambiguous/unresolved evidence is omitted; absence is not proof of no owner.
    This retains the batched legacy API without choosing an arbitrary product.
    """
    return {
        repo: result.owner
        for repo, result in (await resolve_repo_ownership(db, repos)).items()
        if result.state == "unique" and result.owner is not None
    }


class OwnershipConflict(ValueError):
    """Fresh ownership evidence is insufficient to authorize this mutation."""

    def __init__(self, state: str, reason: str):
        self.state = state
        super().__init__(f"Repository ownership {state}: {reason}")

    def result(self) -> dict:
        return {"ok": False, "step": "ownership", "ownershipState": self.state,
                "error": str(self)}


@dataclass(frozen=True)
class RepoOwnershipSnapshot:
    canonical_repos: Mapping[str, str | None]
    registrations: frozenset[tuple[str | None, str | None, str | None]]


async def _registration_evidence(db: AsyncSession) -> frozenset:
    return frozenset(tuple(row) for row in (await db.execute(
        select(ProjectRepo.repo, Project.id, Project.slug)
        .outerjoin(Project, Project.id == ProjectRepo.project_id)
    )).all())


async def gather_repo_identities(
    candidates: Collection[str], *, verified_repos: Mapping[str, str | None] | None = None,
) -> dict[str, str | None]:
    """Gather canonical evidence once, outside the mutation transaction."""
    from server.automations.target_identity import resolve_repositories

    supplied = dict(verified_repos or {})
    pending = sorted(set(candidates) - supplied.keys())
    identities = await resolve_repositories(pending) if pending else {}
    identities.update(supplied)
    return identities


async def gather_repo_ownership_snapshot(repos: Collection[str]) -> RepoOwnershipSnapshot:
    from server.db import db_session

    async with db_session() as db:
        registrations = await _registration_evidence(db)
    identities = await gather_repo_identities(
        set(repos) | {repo for repo, _, _ in registrations if repo})
    return RepoOwnershipSnapshot(identities, registrations)


async def require_mutation_owner(
    db: AsyncSession, repo: str, *, snapshot: RepoOwnershipSnapshot | None,
    allow_missing: bool = False, project_id: str | None = None,
) -> RepoOwner | None:
    """Revalidate gathered registration evidence before any mutation; never network.

    Missing evidence is a refusal even for direct service callers. Verified absence
    is supported only by explicit standalone human paths, never fleet authority.
    """
    if snapshot is None:
        raise OwnershipConflict("unresolved", "Canonical preflight is required.")
    if await _registration_evidence(db) != snapshot.registrations:
        raise OwnershipConflict("changed", "Product registrations changed; retry preflight.")
    ownership = (await resolve_repo_ownership(
        db, [repo], canonical_repos=snapshot.canonical_repos)).get(repo)
    if ownership is None:
        raise OwnershipConflict("unresolved", "Repository is missing.")
    if ownership.state == "missing" and allow_missing and project_id is None:
        return None
    if ownership.state != "unique" or ownership.owner is None:
        raise OwnershipConflict(ownership.state, ownership.reason)
    if project_id is not None and ownership.owner.project_id != project_id:
        raise OwnershipConflict("mismatch", "Repository belongs to another product.")
    return ownership.owner


async def lock_repo_registration(db: AsyncSession) -> None:
    """Serialize registration writers across processes until transaction completion.

    Must be the first SQL in the request session, after canonical network preflight.
    PostgreSQL's table lock also excludes mirror rename writes; SQLite reserves its
    single writer before any reads, avoiding read-to-write upgrade races. Neither
    lock modifies legacy ambiguous registrations or requires a uniqueness rollout.
    """
    if db.in_transaction():
        raise RuntimeError("Repository registration requires a fresh transaction")
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        await db.execute(text("LOCK TABLE project_repos IN SHARE ROW EXCLUSIVE MODE"))
    elif dialect == "sqlite":
        await db.execute(text("BEGIN IMMEDIATE"))
    else:
        raise RuntimeError(f"Unsupported repository registration database: {dialect}")


async def require_unregistered_repos(
    db: AsyncSession, repos: Collection[str], *, snapshot: RepoOwnershipSnapshot,
) -> dict[str, str]:
    """Under the registration lock, prove absence and return verified identities."""
    if await _registration_evidence(db) != snapshot.registrations:
        raise OwnershipConflict("changed", "Product registrations changed; retry registration.")
    ownership = await resolve_repo_ownership(db, repos, canonical_repos=snapshot.canonical_repos)
    identities: dict[str, str] = {}
    for repo, result in ownership.items():
        if result.state != "missing" or result.canonical_repo is None:
            owners = ", ".join(owner.slug for owner in result.candidates)
            unavailable = ", ".join(result.unresolved_repos)
            reason = f"{repo}: {result.reason}"
            if owners:
                reason += f" Existing products: {owners}. Choose another repository or correct its registration."
            if unavailable:
                reason += f" Unavailable registrations: {unavailable}. Verify access and retry."
            raise OwnershipConflict(result.state, reason)
        identities[repo] = result.canonical_repo
    return identities
