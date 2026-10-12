"""Human issue edits and close decisions through the canonical PM write journal.

GitHub has no conditional updateIssue: preflight detects changes since review,
while the remaining external read/write race cannot be eliminated by our lock.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime
from typing import Literal, Self, get_args
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from server.db.models import WorkItem

from . import journal
from .outcomes import escalation_for, escalation_to_wire
from .gh_mutations import build_batch, build_issue_query, parse_batch
from .materialize_apply import Mutate, error_text
from .reconcile_diff import Mutation
from .repo_project import (OwnershipConflict, RepoOwner, RepoOwnershipSnapshot,
                           require_mutation_owner)
from .sync_request import request_project_sync
from .triage_accept import _aware


class IssueDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["edit", "resolve", "dismiss"]
    version: str = Field(min_length=1, max_length=128)
    title: str | None = Field(default=None, max_length=256)
    body: str | None = Field(default=None, max_length=65536)

    @model_validator(mode="after")
    def validate_edit(self) -> Self:
        if self.action == "edit" and (not self.title or not self.title.strip() or self.body is None):
            raise ValueError("Editing requires a non-empty title and a body")
        return self


def detail(src: WorkItem) -> dict:
    title, body, state = src.title or "", src.body or "", src.state or "open"
    version = hashlib.sha256(json.dumps([title, body, state]).encode()).hexdigest()
    held_at = _aware(src.hold_set_at)
    return dict(id=src.id, repo=src.repo, number=src.number, title=title, body=body,
                state=state, url=src.url, version=version, ref=f"{src.repo}#{src.number}",
                holdKind=src.hold_kind, holdReason=src.hold_reason, holdBy=src.hold_by,
                holdSetAt=held_at.isoformat() if held_at else None,
                # Same reader the holds list uses, so the dialog and the card agree
                # about which holds carry an "Approve escalation" button.
                escalation=escalation_to_wire(escalation_for(src.hold_kind, src.hold_reason)))


def _issue(payload: object, node_id: str) -> dict:
    if (not isinstance(payload, dict) or payload.get("id") != node_id
            or not isinstance(payload.get("title"), str)
            or not isinstance(payload.get("body"), str)
            or payload.get("state") not in {"OPEN", "CLOSED"}):
        raise ValueError("GitHub did not return the requested issue; check access and retry")
    return payload


def _mirror(src: WorkItem, issue: dict) -> None:
    src.title, src.body, src.state = issue["title"], issue["body"], issue["state"].lower()
    if isinstance(issue.get("url"), str):
        src.url = issue["url"]


async def _fetch_remote(src: WorkItem, fetch: Mutate) -> dict | None:
    """The issue as GitHub has it now, mirrored onto *src*; None when unreadable."""
    owner = (src.repo or "").partition("/")[0]
    try:
        payload = await fetch(owner, build_issue_query(str(src.gh_node_id)))
        if payload.get("errors"):
            raise ValueError(error_text(payload))
        remote = _issue((payload.get("data") or {}).get("node"), str(src.gh_node_id))
    except Exception:
        return None
    _mirror(src, remote)
    return remote


_FETCH_FAILED = {"ok": False, "step": "fetch",
                 "error": "Could not read issue from GitHub; check access and retry"}


async def _write(db: AsyncSession, *, src: WorkItem, mutation: Mutation, batch: str,
                 confirms: Callable[[dict], bool], mutate: Mutate, now: datetime,
                 project: RepoOwner | None) -> dict:
    """Journal *mutation* ahead, send it, accept it only if *confirms* the returned
    issue, then mirror it. The one write tail every issue decision shares."""
    row_id = (await db.run_sync(lambda s: journal.record_pending(
        s, [mutation], batch_id=f"{batch}:{uuid4()}", now=now, dry_run=False)))[0]
    await db.commit()
    doc, aliases = build_batch([mutation], project_node_id="", field_cache={})
    owner = (src.repo or "").partition("/")[0]
    try:
        payload = await mutate(owner, doc)
        error = parse_batch(payload, aliases).errors.get("m0")
        if error:
            raise ValueError(error)
        confirmed = _issue(((payload.get("data") or {}).get("m0") or {}).get("issue"), str(src.gh_node_id))
        if not confirms(confirmed):
            raise ValueError("GitHub did not confirm the requested change")
    except Exception:
        # Do not echo transport exceptions: credential-bearing upstream messages
        # are not suitable for the audit log or a user-visible response.
        error = "GitHub did not confirm the change; reload and check access before retrying"
        await db.run_sync(lambda s: journal.mark_failed(s, row_id, error, now))
        return {"ok": False, "step": "mutate", "error": error}
    _mirror(src, confirmed)
    await db.run_sync(lambda s: journal.mark_applied(s, [row_id], now))
    if project is not None:
        await request_project_sync(db, project.project_id, now)
    return {"ok": True, "item": detail(src)}


async def apply(db: AsyncSession, *, src: WorkItem, decision: IssueDecision,
                fetch: Mutate, mutate: Mutate, now: datetime,
                ownership: RepoOwnershipSnapshot | None = None) -> dict:
    """Write ahead, confirm the returned issue, then update mirror. Caller commits."""
    try:
        project = await require_mutation_owner(db, src.repo or "", snapshot=ownership, allow_missing=True)
    except OwnershipConflict as exc:
        return exc.result()
    if decision.version != detail(src)["version"] or src.state != "open":
        return {"ok": False, "step": "conflict", "error": "Issue changed; reload before deciding"}
    if not src.gh_node_id:
        return {"ok": False, "step": "node_id", "error": "Issue has no GitHub node id; wait for sync"}
    if await _fetch_remote(src, fetch) is None:
        return _FETCH_FAILED
    if decision.version != detail(src)["version"]:
        return {"ok": False, "step": "conflict", "error": "Issue changed on GitHub; reload before deciding"}
    values = ({"title": decision.title, "body": decision.body} if decision.action == "edit"
              else {"stateReason": "COMPLETED" if decision.action == "resolve" else "NOT_PLANNED"})
    mutation = Mutation("update_issue" if decision.action == "edit" else "close_issue",
                        src.gh_node_id, None,
                        {"title": src.title, "body": src.body, "state": src.state}, values,
                        f"issue-{decision.action}:{src.repo}#{src.number}")

    def confirms(issue: dict) -> bool:
        if decision.action == "edit":
            return issue["title"] == decision.title and issue["body"] == decision.body
        return issue["state"] == "CLOSED" and issue.get("stateReason") == values["stateReason"]

    return await _write(db, src=src, mutation=mutation, batch="issue-decision", confirms=confirms,
                        mutate=mutate, now=now, project=project)


#: Why an issue is closed by request — THE vocabulary the endpoint, the assistant's
#: ``close_issue`` and this module share (upper-cased, GitHub's ``stateReason``).
CloseReason = Literal["completed", "not_planned", "duplicate"]
CLOSE_REASONS: tuple[str, ...] = get_args(CloseReason)


class IssueClose(BaseModel):
    """Close one open issue with a stated reason (the assistant's ``close_issue``).

    Unlike ``IssueDecision`` there is no review ``version``: the caller asked by ref,
    not from a dialog showing a snapshot, so "it changed since you looked" has no
    meaning — an issue that is no longer open is the only conflict."""
    model_config = ConfigDict(extra="forbid")
    reason: CloseReason
    #: Work item id of the canonical issue; required for, and only for, ``duplicate``.
    duplicate_of: str | None = None

    @model_validator(mode="after")
    def validate_duplicate(self) -> Self:
        if (self.reason == "duplicate") != bool(self.duplicate_of):
            raise ValueError("duplicate_of is required for a duplicate close, and only for one")
        return self


async def close(db: AsyncSession, *, src: WorkItem, request: IssueClose,
                fetch: Mutate, mutate: Mutate, now: datetime,
                ownership: RepoOwnershipSnapshot | None = None) -> dict:
    """Close *src* with ``request.reason`` through the journal. Caller commits."""
    try:
        project = await require_mutation_owner(db, src.repo or "", snapshot=ownership, allow_missing=True)
    except OwnershipConflict as exc:
        return exc.result()
    if src.state != "open":
        return {"ok": False, "step": "conflict", "error": "Issue is not open"}
    duplicate_node = None
    if request.duplicate_of is not None:
        dup = await db.get(WorkItem, request.duplicate_of)
        if dup is None or dup.kind != "issue" or dup.id == src.id:
            return {"ok": False, "step": "duplicate", "error": "duplicate_of must be another mirrored issue"}
        if not dup.gh_node_id:
            return {"ok": False, "step": "node_id", "error": "The canonical issue has no GitHub node id; wait for sync"}
        duplicate_node = dup.gh_node_id
    if not src.gh_node_id:
        return {"ok": False, "step": "node_id", "error": "Issue has no GitHub node id; wait for sync"}
    if await _fetch_remote(src, fetch) is None:
        return _FETCH_FAILED
    if src.state != "open":
        return {"ok": False, "step": "conflict", "error": "Issue is already closed on GitHub"}
    values: dict[str, str] = {"stateReason": request.reason.upper()}
    if duplicate_node:
        values["duplicateIssueId"] = duplicate_node
    mutation = Mutation("close_issue", src.gh_node_id, None,
                        {"title": src.title, "body": src.body, "state": src.state}, values,
                        f"issue-close:{src.repo}#{src.number}")
    return await _write(db, src=src, mutation=mutation, batch="issue-close",
                        confirms=lambda issue: (issue["state"] == "CLOSED"
                                                and issue.get("stateReason") == values["stateReason"]),
                        mutate=mutate, now=now, project=project)
