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

from ccswitch import gitrepos
from server.auth.middleware import current_identity

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
    orch = request.app.state.sessions
    names = [repo.get("nameWithOwner") or "" for repo in repos]
    cloned = set(await orch.cloned_repos(names))
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
