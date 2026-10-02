"""Human issue edits and close decisions through the canonical PM write journal.

GitHub has no conditional updateIssue: preflight detects changes since review,
while the remaining external read/write race cannot be eliminated by our lock.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal, Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from server.db.models import WorkItem

from . import journal
from .outcomes import escalation_for, escalation_to_wire
from .gh_mutations import build_batch, build_issue_query, parse_batch
from .materialize_apply import Mutate, error_text
from .reconcile_diff import Mutation
from .repo_project import OwnershipConflict, RepoOwnershipSnapshot, require_mutation_owner
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
    owner = (src.repo or "").partition("/")[0]
    try:
        payload = await fetch(owner, build_issue_query(src.gh_node_id))
        if payload.get("errors"):
            raise ValueError(error_text(payload))
        remote = _issue((payload.get("data") or {}).get("node"), src.gh_node_id)
    except Exception:
        return {"ok": False, "step": "fetch", "error": "Could not read issue from GitHub; check access and retry"}
    _mirror(src, remote)
    if decision.version != detail(src)["version"]:
        return {"ok": False, "step": "conflict", "error": "Issue changed on GitHub; reload before deciding"}
    values = ({"title": decision.title, "body": decision.body} if decision.action == "edit"
              else {"stateReason": "COMPLETED" if decision.action == "resolve" else "NOT_PLANNED"})
    mutation = Mutation("update_issue" if decision.action == "edit" else "close_issue",
                        src.gh_node_id, None,
                        {"title": src.title, "body": src.body, "state": src.state}, values,
                        f"issue-{decision.action}:{src.repo}#{src.number}")
    row_id = (await db.run_sync(lambda s: journal.record_pending(
        s, [mutation], batch_id=f"issue-decision:{uuid4()}", now=now, dry_run=False)))[0]
    await db.commit()
    doc, aliases = build_batch([mutation], project_node_id="", field_cache={})
    try:
        payload = await mutate(owner, doc)
        error = parse_batch(payload, aliases).errors.get("m0")
        if error:
            raise ValueError(error)
        confirmed = _issue(((payload.get("data") or {}).get("m0") or {}).get("issue"), src.gh_node_id)
        if decision.action == "edit":
            matches = confirmed["title"] == decision.title and confirmed["body"] == decision.body
        else:
            matches = confirmed["state"] == "CLOSED" and confirmed.get("stateReason") == values["stateReason"]
        if not matches:
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
