"""Remove one blocked-by edge on GitHub, journaled (roadmap spec §8/§10, #1472).

The human disposition for a `blocked` issue leaf. Shaped like
``materialize_apply``: the write is journaled BEFORE the call, the payload comes
back through the same ``mutate`` seam, and the local ``WorkItemDep`` rows are
deactivated only once GitHub has agreed — the map's tier reads those rows, so a
refused removal leaves the row red rather than lying green for a sync cycle.

The local deactivation is only what makes the leaf flip NOW. What makes the
removal durable is the applied journal row: the mirror re-derives ``order`` and
``prose`` dep edges from the epic's sibling order and the issue body on every
sync pass, so ``mirror_store.upsert_relations`` checks the same applied
``remove_blocked_by`` key before it creates or reactivates one (the counterpart
of ``reconcile_diff.compile_edges`` on the GitHub side). That suppression is
deliberately scoped to the mirror's own re-derivation: a human who re-adds the
dependency on GitHub produces a ``native`` edge, which is a newer decision than
our removal and is NOT suppressed.
"""
from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.db.models import WorkItem, WorkItemDep

from . import journal
from .gh_mutations import build_batch, parse_batch
from .materialize_apply import Mutate, error_text
from .reconcile_diff import remove_blocked_by_mutation
from .refs import split_ref
from .repo_project import OwnershipConflict, RepoOwnershipSnapshot, require_mutation_owner
from .sync_request import request_project_sync

INITIATED_BY_PREFIX = "dep-remove:"


async def remove(db: AsyncSession, *, src: WorkItem, blocker_ref: str,
                 mutate: Mutate, now: datetime,
                ownership: RepoOwnershipSnapshot | None = None) -> dict:
    """Delete the native edge ``src`` ← blocked by ← ``blocker_ref``.

    Returns ``{"ok": True, "src", "blocker"}`` or ``{"ok": False, "step", "error"}``
    with ``step`` in ``resolve`` (blocker not mirrored), ``node_id`` (a side has
    no GitHub node id yet) or ``mutate`` (GitHub refused). Does not commit.

    Crash window: a crash between ``mark_applied`` and the caller's commit leaves
    the journal row ``interrupted`` and the local rows still active. GitHub's
    state is the truth there (the edge is gone) and a second Remove tap repairs
    ViberPit's view — there is deliberately no resume path, unlike
    ``materialize_apply``, because re-issuing the removal is idempotent and costs
    nothing, while a half-applied issue creation is not."""
    try:
        owner_row = await require_mutation_owner(db, src.repo or "", snapshot=ownership, allow_missing=True)
    except OwnershipConflict as exc:
        return exc.result()
    src_ref = f"{src.repo}#{src.number}"
    parsed = split_ref(blocker_ref)
    dst = None
    if parsed is not None:
        dst = (await db.execute(select(WorkItem).where(
            WorkItem.repo == parsed[0], WorkItem.number == parsed[1],
            WorkItem.kind == "issue"))).scalar_one_or_none()
    if dst is None:
        return {"ok": False, "step": "resolve", "error": f"blocker {blocker_ref} is not mirrored"}
    for item, ref in ((src, src_ref), (dst, blocker_ref)):
        if not item.gh_node_id:
            return {"ok": False, "step": "node_id",
                    "error": f"{ref} has no GitHub node id yet; wait for the next sync"}
    mutation = remove_blocked_by_mutation(
        str(src.gh_node_id), str(dst.gh_node_id),
        initiated_by=f"{INITIATED_BY_PREFIX}{src_ref}->{blocker_ref}")
    batch_id = f"{INITIATED_BY_PREFIX}{uuid4()}"
    row_id = (await db.run_sync(lambda s: journal.record_pending(
        s, [mutation], batch_id=batch_id, now=now, dry_run=False)))[0]
    await db.commit()  # write-ahead: the row must outlive a crash mid-call
    doc, alias_map = build_batch([mutation], project_node_id="", field_cache={})
    owner = str(src.repo or "").partition("/")[0]
    try:
        payload = await mutate(owner, doc)
    except Exception as e:  # transport failure — still a recorded attempt
        payload = {"errors": [{"message": str(e)}]}
    error = parse_batch(payload, alias_map).errors.get("m0") or ""
    if error:
        error = error if error != "no data returned" else (error_text(payload) or error)
        await db.run_sync(lambda s: journal.mark_failed(s, row_id, error, now))
        return {"ok": False, "step": "mutate", "error": error}
    await db.run_sync(lambda s: journal.mark_applied(s, [row_id], now))
    rows = (await db.execute(select(WorkItemDep).where(
        WorkItemDep.src_ref == src_ref, WorkItemDep.dst_ref == blocker_ref,
        WorkItemDep.active.is_(True)))).scalars().all()
    for row in rows:
        row.active = False
    if owner_row is not None:
        await request_project_sync(db, owner_row.project_id, now)
    return {"ok": True, "src": src_ref, "blocker": blocker_ref}
