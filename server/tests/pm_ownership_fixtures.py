"""Canonical GitHub evidence for isolated PM mutation unit tests."""
from server.pm.repo_project import RepoOwnershipSnapshot, _registration_evidence


async def identity_repositories(repos):
    return {repo: repo for repo in repos}


async def ownership_snapshot(db, *repos):
    registrations = await _registration_evidence(db)
    names = set(repos) | {repo for repo, _, _ in registrations if repo}
    return RepoOwnershipSnapshot({repo: repo for repo in names}, registrations)
