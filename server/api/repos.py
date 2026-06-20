"""GitHub repos API — list repos the user's gh creds can access, list branches.

These replace the old local-scan ``/api/repos`` endpoints.  ccswitch no longer
scans the user's local ``~/GitHub`` checkouts; it lists the GitHub repos the
user's ``gh`` credentials can access and clones-on-select into a ccswitch-owned
namespace under ``<repo_root>/.ccswitch/clones``.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query

from ccswitch import gitrepos
from server.auth.middleware import current_identity
from server.config import get_settings

router = APIRouter(prefix="/api/github", tags=["github"])


@router.get("/repos")
async def list_github_repos(
    _identity: str = Depends(current_identity),
) -> list[dict]:
    """List GitHub repos accessible to the user's gh creds (pushedAt-desc).

    Each repo is annotated with ``cloned`` — whether a ccswitch-owned clone
    already exists under ``<repo_root>/.ccswitch/clones/<owner>/<name>``.
    """
    settings = get_settings()
    try:
        repos = await asyncio.to_thread(gitrepos.list_accessible_repos)
    except gitrepos.GhError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    base = gitrepos.clones_base(settings.repo_root)
    for repo in repos:
        name_with_owner = repo.get("nameWithOwner") or ""
        owner, _, name = name_with_owner.partition("/")
        repo["cloned"] = bool(owner and name and (base / owner / name / ".git").exists())
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
