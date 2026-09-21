"""Applying an accepted materialization proposal (spec 003 §7b, #992).

Session-level orchestration of the mechanical half of materialization: create the
epic, create each task as a sub-issue of it, then add the blocked-by chain. Every
GitHub write is journaled BEFORE it is sent (spec §6) and nothing is ever retried
blind — the created node id recorded on the proposal is the resume key, so a
re-run after a failure picks up exactly where the last one stopped instead of
creating a second copy of everything that already exists.

What this module deliberately does NOT write: board membership, Status, Stage and
Score. The reconciler owns every board field; ``request_project_sync`` at the end
is how they land, one mirror pass later.

The journal helpers are synchronous over a ``Session``, so they run through
``db.run_sync``; the commits between steps are not bookkeeping, they are what
makes the write-ahead row survive a crash during the call it is describing.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from copy import deepcopy
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.db.models import Initiative, MaterializationProposal, WorkItem
from server.sdlc.engine import advance_stage

from . import journal
from .gh_mutations import _q, build_batch, chunk, parse_batch
from .materialize import (
    Decomposition,
    MaterializationParseError,
    TaskSpec,
    blocked_by_mutation,
    build_create_issue,
    chain_edges,
)
from .reconcile_diff import Mutation
from .refs import split_ref
from .repo_project import OwnershipConflict, RepoOwnershipSnapshot, require_mutation_owner
from .score import rice_score
from .sync_request import request_project_sync

log = logging.getLogger(__name__)

#: ``(owner, document) -> GraphQL payload``. The orchestrator passes its own
#: ``_pm_reconcile_mutate`` (App/PAT ladder, ``partial_ok``), so a per-alias
#: rejection comes back as a payload to read rather than an exception to guess at.
Mutate = Callable[[str, str], Awaitable[dict]]

INITIATED_BY_PREFIX = "materialize:"


def _decomposition(payload: dict) -> Decomposition:
    """The stored proposal payload as the pure ``Decomposition`` again."""
    tasks = payload.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise MaterializationParseError("proposal has no tasks")
    return Decomposition(
        epic_title=str(payload.get("epic_title") or ""),
        epic_body=str(payload.get("epic_body") or ""),
        tasks=tuple(TaskSpec(title=str(t.get("title") or ""),
                             body=str(t.get("body") or ""),
                             depends_on=tuple(t.get("depends_on") or ()))
                    for t in tasks))


def error_text(payload: dict) -> str:
    """First human-readable message in a GraphQL document-level ``errors`` block.

    Public because ``dep_remove`` reads the same payload shape."""
    errors = payload.get("errors") or []
    for err in errors:
        message = str((err or {}).get("message") or "").strip()
        if message:
            return message
    return ""


async def apply(db: AsyncSession, proposal: MaterializationProposal, *,
                mutate: Mutate, now: datetime,
                ownership: RepoOwnershipSnapshot | None = None) -> dict:
    """Create the epic, its task sub-issues and the chain for *proposal*.

    Returns ``{"ok": True, …counts}`` or ``{"ok": False, "step": …, "error": …}``.
    A failure leaves the proposal ``pending`` (so the same accept can be tapped
    again once the cause is fixed), the initiative ``blocked``, and the journal
    carrying exactly one failed row for the call that failed.
    """
    ini = (await db.execute(select(Initiative).where(
        Initiative.id == proposal.initiative_id))).scalar_one_or_none()
    if ini is None:
        return {"ok": False, "step": "load",
                "error": f"proposal {proposal.id} has no initiative"}
    repo = str(ini.repo or "")
    try:
        product = await require_mutation_owner(
            db, repo, snapshot=ownership, allow_missing=ini.project_id is None,
            project_id=ini.project_id)
    except OwnershipConflict as exc:
        return exc.result()
    owner, _, name = repo.partition("/")
    slug = str(ini.slug or "")
    initiated_by = f"{INITIATED_BY_PREFIX}{slug}"
    batch_id = f"{INITIATED_BY_PREFIX}{proposal.id}"

    async def fail(step: str, error: str) -> dict:
        ini.status = "blocked"
        await db.commit()
        log.warning("materialize.apply failed slug=%s step=%s: %s", slug, step, error)
        return {"ok": False, "step": step, "error": error}

    try:
        decomposition = _decomposition(proposal.payload or {})
        # Computed BEFORE anything is created: a cyclic or out-of-range chain must
        # fail with nothing on GitHub, not with a half-built epic.
        edges = chain_edges(decomposition)
    except MaterializationParseError as e:
        return await fail("payload", str(e))
    if not owner or not name:
        return await fail("payload", f"initiative {slug} has no owner/name repo")

    created: dict = dict((proposal.payload or {}).get("created") or {})
    created.setdefault("tasks", {})

    async def remember() -> None:
        """Persist the resume key, immediately: an id we hold but never wrote is an
        issue the next run creates a second time.

        The DEEP COPY is load-bearing. A JSON column is compared by value at flush
        time, so assigning a structure that still aliases the last committed one
        makes the flush see "no change" and skip the UPDATE — every id after the
        first would be lost, which is exactly the duplicate-issue bug this row is
        here to prevent."""
        proposal.payload = {**(proposal.payload or {}), "created": deepcopy(created)}
        await db.commit()

    def create_mutation(target_node_id: str, title: str) -> Mutation:
        return Mutation("create_issue", target_node_id, None, None, title, initiated_by)

    async def create_issue(mutation: Mutation, body: str, *,
                           parent: str | None) -> tuple[dict | None, str]:
        """Journal row → ``createIssue`` → mark. ``(issue, "")`` or ``(None, error)``."""
        title = str(mutation.new_value)
        row_id = (await db.run_sync(lambda s: journal.record_pending(
            s, [mutation], batch_id=batch_id, now=now, dry_run=False)))[0]
        await db.commit()  # write-ahead: the row must outlive a crash mid-call
        doc = build_create_issue(repository_id, title, body, parent_issue_id=parent)
        try:
            payload = await mutate(owner, doc)
        except Exception as e:  # transport failure — still a recorded attempt
            payload = {"errors": [{"message": str(e)}]}
        issue = ((payload.get("data") or {}).get("createIssue") or {}).get("issue") or {}
        if not issue.get("id"):
            error = error_text(payload) or "createIssue returned no issue"
            await db.run_sync(lambda s: journal.mark_failed(s, row_id, error, now))
            return None, error
        await db.run_sync(lambda s: journal.mark_applied(s, [row_id], now))
        return {"node_id": issue["id"], "number": issue.get("number"),
                "url": issue.get("url")}, ""

    # --- repository id ------------------------------------------------------ #
    # Remembered with the rest of the resume state: every createIssue needs it,
    # so a resume would otherwise spend a rate point re-asking for a constant.
    repository_id = str(created.get("repository_id") or "")
    if not repository_id:
        doc = ("query { repository(owner: %s, name: %s) { id } }"
               % (_q(owner), _q(name)))
        try:
            payload = await mutate(owner, doc)
        except Exception as e:
            return await fail("repository", str(e))
        repository_id = str((((payload.get("data") or {}).get("repository")) or {})
                            .get("id") or "")
        if not repository_id:
            return await fail("repository",
                              error_text(payload) or f"cannot resolve {repo}")
        created["repository_id"] = repository_id
        await remember()

    # Keys whose outcome a previous run never learned (it crashed between the call
    # and the commit that would have recorded the id). `createIssue` is not
    # idempotent, so re-issuing one of these would create a SECOND issue — the one
    # thing the journal exists to prevent. Stop and ask for a human instead.
    unresolved = await db.run_sync(lambda s: journal.unresolved_keys(s, batch_id))

    def ambiguous(mutation: Mutation, what: str) -> str:
        if mutation.key() not in unresolved:
            return ""
        return (f"a previous attempt to create {what} never reported back (its "
                f"journal row is still pending); it may exist on {repo} already — "
                f"reconcile the journal with the repo by hand, then retry")

    # --- the epic ----------------------------------------------------------- #
    epic: dict = dict(created.get("epic") or {})
    if not epic.get("node_id"):
        mutation = create_mutation(repository_id, decomposition.epic_title)
        doubt = ambiguous(mutation, "the epic")
        if doubt:
            return await fail("resume_ambiguous", doubt)
        issue, error = await create_issue(mutation, decomposition.epic_body,
                                          parent=None)
        if issue is None:
            return await fail("epic", error)
        created["epic"] = epic = issue
        await remember()

    # --- the tasks, in plan order ------------------------------------------- #
    # ``parentIssueId`` makes each task a sub-issue in the SAME call (probe-verified),
    # so there is no separate addSubIssue write and no fresh-id window to lose.
    task_nodes: dict[str, dict] = dict(created.get("tasks") or {})
    for idx, task in enumerate(decomposition.tasks):
        if (task_nodes.get(str(idx)) or {}).get("node_id"):
            continue  # already created by an earlier run — never re-issue it
        mutation = create_mutation(str(epic["node_id"]), task.title)
        doubt = ambiguous(mutation, f"task {idx} ({task.title!r})")
        if doubt:
            return await fail("resume_ambiguous", doubt)
        issue, error = await create_issue(mutation, task.body,
                                          parent=str(epic["node_id"]))
        if issue is None:
            return await fail(f"task:{idx}", error)
        task_nodes[str(idx)] = issue
        created["tasks"] = task_nodes
        await remember()

    # --- the blocked-by chain ----------------------------------------------- #
    # Built as the reconciler's OWN Mutation so the journal key matches the one
    # ``compile_edges`` would compute: once applied here, ``applied_keys``
    # suppresses the edge on every future reconcile pass (and on a resume here).
    mutations = [blocked_by_mutation(str(task_nodes[str(blocked)]["node_id"]),
                                     str(task_nodes[str(blocker)]["node_id"]),
                                     initiated_by=initiated_by)
                 for blocked, blocker in edges]
    already = await db.run_sync(journal.applied_keys)
    # An edge parked by an earlier accept is a decided outcome, not a pending one:
    # re-sending it every retry would strand the whole materialization on one bad
    # relation forever. Same rule, same helpers as the reconciler.
    parked = await db.run_sync(
        lambda s: {m.key() for m in mutations if journal.is_parked(s, m.key())})
    pending = [m for m in mutations
               if m.key() not in already and m.key() not in parked]
    failures: list[str] = []
    transient = ""

    async def fail_batch_transiently(row_ids: list[str], reason: str) -> None:
        """Every row in the batch failed for a reason that judges none of them.

        A transport error, a 502, an abuse limit or a document-level GraphQL error
        is copied onto every alias by ``parse_batch``. Recorded as ordinary
        failures, two such blips would PARK the whole chain — real edges never
        created and suppressed forever after. The marker keeps them visible in the
        journal but out of ``failure_count`` (same rule as the reconciler)."""
        for rid in row_ids:
            await db.run_sync(lambda s: journal.mark_failed(
                s, rid, journal.TRANSIENT_PREFIX + reason, now))
        await db.commit()

    for group in chunk(pending):  # build_batch caps a document at 20 aliases
        row_ids = await db.run_sync(lambda s: journal.record_pending(
            s, group, batch_id=batch_id, now=now, dry_run=False))
        await db.commit()
        # add_blocked_by ignores both project arguments (see gh_mutations._render);
        # there is no board in play here, only issue-to-issue relations.
        doc, alias_map = build_batch(group, project_node_id="", field_cache={})
        try:
            payload = await mutate(owner, doc)
        except Exception as e:
            transient = str(e)
            await fail_batch_transiently(row_ids, transient)
            break
        result = parse_batch(payload, alias_map)
        if result.failed_transiently():
            transient = result.document_error or "the mutation document failed"
            await fail_batch_transiently(row_ids, transient)
            break
        for (alias, mutation), rid in zip(alias_map.items(), row_ids, strict=True):
            error = result.errors.get(alias) or ""
            # ``converged`` is the other half of the reconciler's rule this loop
            # mirrors: a rejection that only reports the edge is already there means
            # the chain holds, so it is applied, not a failure (#1947). A resume
            # re-sends any edge whose row is still pending/failed, which is exactly
            # when GitHub answers that way — and failing here sets the initiative
            # ``blocked`` and burns one of MATERIALIZE_MAX_ATTEMPTS.
            if not error or alias in result.converged:
                await db.run_sync(lambda s: journal.mark_applied(s, [rid], now))
                continue
            # Park on the SECOND failure on its own merits, exactly as the
            # reconciler does: one bad relation (a cycle GitHub refuses, a deleted
            # issue) must not hold the epic, its tasks and every other edge
            # hostage forever. The first failure still blocks, so a transient
            # refusal gets its retry.
            if await db.run_sync(lambda s: journal.failure_count(s, mutation.key())) >= 1:
                await db.run_sync(
                    lambda s: journal.park(s, rid, f"failed twice: {error}", now))
                parked.add(mutation.key())
            else:
                failures.append(f"{alias}: {error}")
                await db.run_sync(lambda s: journal.mark_failed(s, rid, error, now))
        await db.commit()
    if transient:
        # Retryable by construction: nothing was judged, so the same accept can be
        # tapped again and will re-send exactly these edges.
        return await fail("edges_transient", transient)
    if failures:
        return await fail("edges", "; ".join(failures)[:500])

    # --- ViberPit-side overlay ---------------------------------------------- #
    ini.epic_repo, ini.epic_number = repo, epic.get("number")
    # Materialize is the plan stage's TERMINAL action (spec §7b), so the confirmed
    # accept IS the human gate for it: the stage moves here rather than parking the
    # initiative at ``plan`` behind a second Advance tap that asks the same question
    # a human just answered — with the epic and its issues already on GitHub, there
    # is nothing left for the plan stage to decide. The successor comes from the
    # engine's own sequence (one definition of "what follows plan"), and a proposal
    # applied from any other stage leaves the stage alone rather than skipping one.
    if ini.stage == "plan":
        ini.stage = advance_stage(ini.stage)
    ini.status = "active"
    # Written as an upsert on (repo, number, kind), the table's own identity: a
    # resumed run — or a pm-sync pass that mirrored the new issues between the
    # failure and the retry — must not hit the unique constraint, and a mirrored
    # row still needs the classified overlay it would otherwise never get.
    numbers = [n.get("number") for n in task_nodes.values() if n.get("number")]
    by_number = {w.number: w for w in (await db.execute(select(WorkItem).where(
        WorkItem.repo == repo, WorkItem.kind == "issue",
        WorkItem.number.in_(numbers)))).scalars().all()}
    # Spec 2026-09-14 D6: seed each task's RICE from the initiative's source issue
    # (the intake or adopted issue in ``origin_ref``) when it is mirrored and fully
    # scored. The epic itself was created seconds ago and has no score yet; with no
    # scored source the tasks stay unscored — the queue never invents numbers.
    seed: tuple[int, int, int, int] | None = None
    source_ref = split_ref(ini.origin_ref or "")
    if source_ref is not None:
        src_repo, src_number = source_ref
        source = (await db.execute(select(WorkItem).where(
            WorkItem.repo == src_repo, WorkItem.number == src_number,
            WorkItem.kind == "issue"))).scalar_one_or_none()
        if source is not None and rice_score(source.reach, source.impact, source.effort,
                                             source.confidence) is not None:
            seed = (source.reach, source.impact, source.effort, source.confidence)  # type: ignore[assignment]

    for idx, task in enumerate(decomposition.tasks):
        node = task_nodes[str(idx)]
        item = by_number.get(node.get("number"))
        if item is None:
            item = WorkItem(repo=repo, number=node.get("number"), kind="issue")
            db.add(item)
        item.state, item.title, item.body = "open", task.title, task.body
        item.url, item.gh_node_id = node.get("url"), node.get("node_id")
        # Classified by construction: these issues came out of a plan a human
        # approved, so they are queue-eligible without a triage pass. RICE comes
        # from the source issue when it is scored (see ``seed`` above); otherwise
        # unset.
        item.triage_state, item.classified_by = "classified", "materialize"
        item.classified_at = now
        if seed is not None:
            item.reach, item.impact, item.effort, item.confidence = seed
    proposal.status = "accepted"
    proposal.resolved_at = now
    sync_requested = await _request_sync(db, product.project_id if product else None, repo, now)
    await db.commit()
    log.info("materialize.apply slug=%s epic=%s tasks=%d edges=%d parked=%d",
             slug, epic.get("number"), len(decomposition.tasks), len(edges),
             len(parked))
    # ``stage`` travels in the result so the route can audit the advance it just
    # caused without re-reading a row another session committed.
    return {"ok": True, "epic": epic, "tasks": len(decomposition.tasks),
            "edges": len(edges), "parkedEdges": len(parked),
            "stage": ini.stage, "syncRequested": sync_requested}


async def _request_sync(db: AsyncSession, project_id: str | None, repo: str,
                        now: datetime) -> bool:
    """Use the preflight's canonical owner; verified standalone work has none."""
    if not project_id:
        log.warning("materialize.apply: %s is owned by no product — board "
                    "membership will not be requested", repo)
        return False
    await request_project_sync(db, project_id, now)
    return True
