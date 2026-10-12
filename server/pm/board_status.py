"""A person's board Status for one issue, written to its product's GitHub Project
through the PM write journal (the assistant's ``set_board_status``, Refs #2979).

Same shape as ``issue_disposition``: the write is journaled BEFORE the call, goes
out through the reconciler's own ``mutate`` seam (its App/PAT ladder), and the
mirror is updated only once GitHub has answered. Adding the issue to the board
(``add_item``) when it is not on it yet is a separate first call, because the
Status write needs the item id that call returns.

The Status write is journaled under ``journal.REQUESTED_STATUS_FIELD``, not
``"Status"``: the reconciler reads its own applied ``Status`` rows back as "this
value is ours, not a person's" (``journal.own_applied_values``) and would revert a
human's choice on its next cycle. Under its own label the write is what it is — a
person's decision — and the reconciler's ownership rule ("human edits win until a
newer decision") applies to it exactly as to an edit made on GitHub.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.db.models import GhProject, GhProjectItem, WorkItem

from . import journal
from .desired_state import HELD_KINDS
from .gh_mutations import build_batch, parse_batch
from .materialize_apply import Mutate, error_text
from .queue import is_self_closing_tracker
from .reconcile_diff import Mutation
from .repo_project import OwnershipConflict, RepoOwnershipSnapshot, require_mutation_owner
from .sync_request import request_project_sync

INITIATED_BY_PREFIX = "board-status:"


class BoardStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str = Field(min_length=1, max_length=64)
    add_if_missing: bool = True


def status_options(gh: GhProject | None) -> list[str]:
    """The board's real Status option names, from the mirrored field cache — the
    vocabulary a write is validated against (a product may have renamed or added
    columns, so ``desired_state.STATUS_OPTIONS`` is not the authority here)."""
    spec = ((gh.field_cache or {}) if gh is not None else {}).get("Status") or {}
    options = spec.get("options") or {}
    return [str(name) for name in options] if isinstance(options, dict) else []


def refusal(src: WorkItem) -> str | None:
    """Why *src* cannot take a board Status from a person, or None.

    A PR is not a board item ViberPit plans; a closed issue's column is the
    reconciler's (Done); a self-closing tracker is its workflow's own artifact
    (``queue.is_self_closing_tracker``, the one tracker verdict); a held item's
    Held column is its hold's, released through the hold's own path."""
    ref = f"{src.repo}#{src.number}"
    if src.kind != "issue":
        return f"{ref} is a pull request, not an issue"
    if (src.state or "open") != "open":
        return f"{ref} is closed"
    if is_self_closing_tracker(src.labels or (), src.title or "", author=src.author or ""):
        return f"{ref} is a self-closing tracker; its workflow owns it"
    if src.hold_kind in HELD_KINDS:
        # The board shows Held for these, and moving the card must not stand in for
        # releasing the hold: that has its own rules (who placed it, decisions).
        return f"{ref} is held ({src.hold_kind}); release the hold first"
    return None


async def board_for(db: AsyncSession, project_id: str) -> GhProject | None:
    return (await db.execute(select(GhProject).where(
        GhProject.project_id == project_id))).scalars().first()


async def _send(db: AsyncSession, *, journaled: Mutation, rendered: Mutation, gh: GhProject,
                owner: str, mutate: Mutate, now: datetime) -> tuple[dict, str | None]:
    """Journal one mutation ahead, send it, mark the row. ``(payload, error)``."""
    row_id = (await db.run_sync(lambda s: journal.record_pending(
        s, [journaled], batch_id=f"{INITIATED_BY_PREFIX}{uuid4()}", now=now, dry_run=False)))[0]
    await db.commit()  # write-ahead: the row must outlive a crash mid-call
    doc, aliases = build_batch([rendered], project_node_id=str(gh.gh_node_id),
                               field_cache=gh.field_cache or {})
    try:
        payload = await mutate(owner, doc)
    except Exception:  # transport failure — still a recorded attempt
        # Not echoed: a transport message can carry credentials (issue_disposition).
        payload = {"errors": [{"message": "GitHub call failed; check access and retry"}]}
    error = parse_batch(payload, aliases).errors.get("m0") or ""
    if error:
        error = error if error != "no data returned" else (error_text(payload) or error)
        await db.run_sync(lambda s: journal.mark_failed(s, row_id, error, now))
        return payload, error
    await db.run_sync(lambda s: journal.mark_applied(s, [row_id], now))
    return payload, None


async def apply(db: AsyncSession, *, src: WorkItem, request: BoardStatusRequest,
                mutate: Mutate, now: datetime,
                ownership: RepoOwnershipSnapshot | None = None) -> dict:
    """Put *src* on its product's board (if asked) and set its Status.

    Returns ``{"ok": True, "ref", "status", "previous", "added", "changed"}`` or
    ``{"ok": False, "step", "error"}`` with ``step`` in ``ownership``, ``refused``,
    ``board`` (no mirrored project), ``status`` (not one of the board's options),
    ``node_id``, ``not_on_board`` (and ``add_if_missing`` is off) or ``mutate``."""
    ref = f"{src.repo}#{src.number}"
    try:
        project = await require_mutation_owner(db, src.repo or "", snapshot=ownership)
    except OwnershipConflict as exc:
        return exc.result()
    if project is None:  # only reachable with allow_missing, kept for the type
        return {"ok": False, "step": "ownership", "error": f"{ref} belongs to no product"}
    why = refusal(src)
    if why:
        return {"ok": False, "step": "refused", "error": why}
    gh = await board_for(db, project.project_id)
    if gh is None or not gh.gh_node_id:
        return {"ok": False, "step": "board",
                "error": f"product {project.slug} has no mirrored GitHub Project; request a product sync"}
    options = status_options(gh)
    if request.status not in options:
        return {"ok": False, "step": "status",
                "error": f"{request.status!r} is not a Status on {project.slug}'s board; "
                         f"options: {', '.join(options) or 'none mirrored yet'}"}
    if not src.gh_node_id:
        return {"ok": False, "step": "node_id", "error": f"{ref} has no GitHub node id yet; wait for sync"}
    item = (await db.execute(select(GhProjectItem).where(
        GhProjectItem.gh_project_id == gh.id,
        GhProjectItem.work_item_id == src.id))).scalars().first()
    previous = item.status if item is not None else None
    if item is not None and previous == request.status:
        return {"ok": True, "ref": ref, "status": request.status, "previous": previous,
                "added": False, "changed": False}
    owner = (src.repo or "").partition("/")[0]
    initiated_by = f"{INITIATED_BY_PREFIX}{ref}"
    added = False
    if item is None or not item.gh_item_node_id:
        if not request.add_if_missing:
            return {"ok": False, "step": "not_on_board",
                    "error": f"{ref} is not on {project.slug}'s board"}
        add = Mutation("add_item", str(gh.gh_node_id), None, None, src.gh_node_id, initiated_by)
        payload, error = await _send(db, journaled=add, rendered=add, gh=gh, owner=owner,
                                     mutate=mutate, now=now)
        node = (((payload.get("data") or {}).get("m0") or {}).get("item") or {}).get("id")
        if error or not isinstance(node, str) or not node:
            return {"ok": False, "step": "mutate",
                    "error": error or "GitHub did not return the board item"}
        if item is None:
            item = GhProjectItem(gh_project_id=gh.id, work_item_id=src.id, gh_item_node_id=node)
            db.add(item)
        else:
            item.gh_item_node_id = node
        added = True
    rendered = Mutation("set_field", str(item.gh_item_node_id), "Status", previous,
                        request.status, initiated_by)
    _, error = await _send(db, journaled=replace(rendered, field=journal.REQUESTED_STATUS_FIELD),
                           rendered=rendered, gh=gh, owner=owner, mutate=mutate, now=now)
    if error:
        await request_project_sync(db, project.project_id, now)
        return {"ok": False, "step": "mutate", "error": error, "added": added}
    # The mirror says what GitHub now says, stamped NOW: until the next sync reads the
    # item's real updatedAt back, the reconciler must see this as the newest decision
    # on the item, not a value older than whatever it last decided.
    item.status, item.gh_updated_at = request.status, now
    await request_project_sync(db, project.project_id, now)
    return {"ok": True, "ref": ref, "status": request.status, "previous": previous,
            "added": added, "changed": True}
