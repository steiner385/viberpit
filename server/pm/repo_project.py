"""One batched repo→product ownership resolver for PM and automation callers.

Identity snapshots are gathered before entering this DB boundary. Registration-only
compatibility reads cannot establish renamed aliases or verified absence.
"""
from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.automations.target_identity import valid_repo_name
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
