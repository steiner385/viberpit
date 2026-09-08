"""GitHub repos API — list repos the user's gh creds can access, list branches.

These replace the old local-scan ``/api/repos`` endpoints.  ccswitch no longer
scans the user's local ``~/GitHub`` checkouts; it lists the GitHub repos the
user's ``gh`` credentials can access and clones-on-select into a ccswitch-owned
namespace under ``<repo_root>/.ccswitch/clones``.
"""
from __future__ import annotations

import asyncio
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

from ccswitch import gitrepos
from server.auth.middleware import current_identity
from server.github import ci_issues
from server.ipc.client import DaemonError, DaemonUnavailable

router = APIRouter(prefix="/api/github", tags=["github"])
log = logging.getLogger(__name__)

# Serve a last-good repos list this long after GitHub goes unreachable, rather than
# erroring. GitHub's transient failures (notably GraphQL rate-limit exhaustion) would
# otherwise 502 on every poll, and the dashboard retries hard enough to turn a brief
# outage into a request storm. The repo list changes rarely, so stale beats an error.
_REPOS_MAX_STALE_SECONDS = 3600.0


@router.get("/repos")
async def list_github_repos(
    request: Request,
    _identity: str = Depends(current_identity),
) -> list[dict]:
    """List GitHub repos accessible to the user's gh creds (pushedAt-desc).

    Each repo is annotated with ``cloned`` — whether a ccswitch-owned clone
    already exists under ``<repo_root>/.ccswitch/clones/<owner>/<name>``.

    On a GitHub failure, falls back to the last-good list (within
    ``_REPOS_MAX_STALE_SECONDS``) so a rate-limit blip doesn't 502-storm the UI.
    """
    state = request.app.state
    try:
        repos = await asyncio.to_thread(gitrepos.list_accessible_repos)
        state.github_repos_cache = (repos, time.monotonic())
    except gitrepos.GhError as exc:
        cache = getattr(state, "github_repos_cache", None)
        if cache is None or (time.monotonic() - cache[1]) > _REPOS_MAX_STALE_SECONDS:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        log.warning(
            "github/repos: gh failed (%s) — serving cached list (%.0fs old)",
            exc, time.monotonic() - cache[1],
        )
        repos = cache[0]

    # The clone check must run where the clones live — the daemon host — not this
    # edge process, whose filesystem has no clones once daemon and edge are split
    # (#709 finding 6). orchestrator.cloned_repos resolves the daemon's own
    # clones_base and returns the subset present there.
    #
    # Best-effort: while the daemon is booting after a restart this RPC raises
    # DaemonUnavailable; uncaught, it killed the whole endpoint and the New Session
    # dropdown showed "— could not load repos —" (2026-08-29 startup-health
    # incident). The list itself comes from gh on the edge, so serve it and degrade
    # `cloned` to the last-good set (else all-False — a repo picked in that window
    # just re-clones on start, which ensure_clone treats as a no-op when it exists).
    orch = request.app.state.sessions
    names = [repo.get("nameWithOwner") or "" for repo in repos]
    try:
        cloned = set(await orch.cloned_repos(names))
        state.cloned_repos_cache = cloned
    except (DaemonUnavailable, DaemonError) as exc:
        cloned = getattr(state, "cloned_repos_cache", set())
        log.warning("github/repos: cloned_repos RPC failed (%s) — serving list with "
                    "%d cached cloned flag(s)", exc, len(cloned))
    for repo in repos:
        repo["cloned"] = (repo.get("nameWithOwner") or "") in cloned
    return repos


@router.get("/branches")
async def list_github_branches(
    repo: str = Query(..., description="nameWithOwner, e.g. owner/name"),
    _identity: str = Depends(current_identity),
) -> dict:
    """Return ``{"default": str|None, "branches": [str]}`` for a remote repo."""
    try:
        return await asyncio.to_thread(gitrepos.list_remote_branches, repo)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except gitrepos.GhError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


class CreateIssueBody(BaseModel):
    repo: str
    title: str = Field(min_length=1, max_length=256)
    body: str = Field(min_length=1, max_length=ci_issues.BODY_MAX_CHARS)
    labels: list[str] | None = None

    @field_validator("repo")
    @classmethod
    def _owner_name(cls, v: str) -> str:
        if not ci_issues.valid_repo(v):
            raise ValueError("repo must be 'owner/name'")
        return v


@router.post("/issues", status_code=201)
async def create_github_issue(
    payload: CreateIssueBody,
    request: Request,
    identity: str = Depends(current_identity),
) -> dict:
    """File a GitHub issue on ``repo`` (spec v2 Pack G).

    ``labels`` defaults to none at all rather than to `ci_issues.create_issue`'s own
    default: that default is the CI/CD Doctor's ``ci-doctor`` label, and an issue filed
    from here is not a doctor finding.
    """
    text = ci_issues.with_authorship(
        payload.body, via=getattr(request.state, "via", None), identity=identity)
    number = await asyncio.to_thread(
        ci_issues.create_issue, payload.repo, payload.title, text,
        labels=payload.labels or [])
    if number is None:
        raise HTTPException(status_code=502, detail="GitHub rejected the issue")
    return {"number": number, "url": ci_issues.issue_url(payload.repo, number)}


@router.get("/credential-alerts")
async def list_credential_alerts(
    request: Request, _identity: str = Depends(current_identity)
) -> dict:
    """Repos whose clone can't fetch due to stale git credentials (issue #670).

    Sourced from the daemon (where ``ensure_clone`` runs and owns the registry),
    so it is correct in remote mode too. The banner polls this; an empty
    ``alerts`` list means every clone is fetching cleanly (the alert auto-cleared
    on a successful fetch)."""
    return await request.app.state.sessions.git_credential_alerts()


@router.delete("/credential-alerts")
async def dismiss_credential_alert(
    request: Request,
    repo: str = Query(..., description="nameWithOwner, e.g. owner/name"),
    _identity: str = Depends(current_identity),
) -> dict:
    """Dismiss one repo's stale-credential alert. Advisory — if the credential
    is still broken, the next failed fetch re-raises it."""
    return await request.app.state.sessions.dismiss_git_credential_alert(repo)
