"""GitHub repos API — list repos the user's gh creds can access, list branches.

These replace the old local-scan ``/api/repos`` endpoints.  ViberPit no longer
scans the user's local ``~/GitHub`` checkouts; it lists the GitHub repos the
user's ``gh`` credentials can access and clones-on-select into a ViberPit-owned
namespace under ``<repo_root>/<namespace>/clones`` (``paths.repo_namespace``).
"""
from __future__ import annotations

import asyncio
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

from viberpit import gitrepos
from server.audit import record_audit
from server.auth.middleware import current_identity
from server.db import get_db
from server.github import ci_issues
from server.ipc.client import DaemonError, DaemonUnavailable

router = APIRouter(prefix="/api/github", tags=["github"])
log = logging.getLogger(__name__)

# Serve a last-good repos list this long after GitHub goes unreachable, rather than
# erroring. GitHub's transient failures (notably GraphQL rate-limit exhaustion) would
# otherwise 502 on every poll, and the dashboard retries hard enough to turn a brief
# outage into a request storm. The repo list changes rarely, so stale beats an error.
_REPOS_MAX_STALE_SECONDS = 3600.0


async def _fetch_repos(state) -> list[dict]:  # noqa: ANN001
    """gh-accessible repos, falling back to the last-good list (within
    ``_REPOS_MAX_STALE_SECONDS``) so a rate-limit blip doesn't 502-storm the UI. 502 if no usable cache."""
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
    return repos


async def known_repo_slugs(app_state) -> set[str]:  # noqa: ANN001
    """Lower-cased ``nameWithOwner`` of every repo the gh creds can access (same cache as the list route)."""
    return {(r.get("nameWithOwner") or "").lower() for r in await _fetch_repos(app_state)} - {""}


@router.get("/repos")
async def list_github_repos(
    request: Request,
    _identity: str = Depends(current_identity),
) -> list[dict]:
    """List GitHub repos accessible to the user's gh creds (pushedAt-desc).

    Each repo is annotated with ``cloned`` — whether a ViberPit-owned clone
    already exists under ``<repo_root>/<namespace>/clones/<owner>/<name>`` (either
    namespace spelling — ``gitrepos.clone_path`` reuses whichever holds it).

    On a GitHub failure, falls back to the last-good list (within
    ``_REPOS_MAX_STALE_SECONDS``) so a rate-limit blip doesn't 502-storm the UI.
    """
    state = request.app.state
    repos = await _fetch_repos(state)

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


class IssueCommentBody(BaseModel):
    body: str = Field(min_length=1, max_length=ci_issues.BODY_MAX_CHARS)


@router.post("/issues/{owner}/{name}/{number}/comments", status_code=201)
async def comment_on_github_issue(
    owner: str,
    name: str,
    number: int,
    payload: IssueCommentBody,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Post a comment on an issue as the operator's gh identity — the issue twin of
    ``POST /api/prs/{owner}/{name}/{number}/comments``.

    Signed when the assistant posts it (`ci_issues.with_authorship`). Deliberately
    WITHOUT ``AUTOMATION_COMMENT_MARKER``: this is a person's word on the issue, so
    the mirror should count it as one (it re-arms a not-needed issue, #2683). A gh
    failure is a 502, never a swallowed success.
    """
    repo = f"{owner}/{name}"
    if not ci_issues.valid_repo(repo):
        raise HTTPException(status_code=422, detail="repo must be 'owner/name'")
    via = getattr(request.state, "via", None)
    text = ci_issues.with_authorship(payload.body, via=via, identity=identity)
    url = await asyncio.to_thread(ci_issues.comment_on_issue, repo, number, text,
                                  automation=False)
    await record_audit(db=db, type="github.issue.comment", identity=identity, via=via,
                       detail={"repo": repo, "number": number, "ok": url is not None,
                               "url": url})
    if url is None:
        await db.commit()  # the failed attempt is exactly what the trail must keep
        raise HTTPException(status_code=502, detail="GitHub rejected the comment")
    return {"url": url}


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
