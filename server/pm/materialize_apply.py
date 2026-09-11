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
from .repo_project import owning_projects_by_repo
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


def _error_text(payload: dict) -> str:
    errors = payload.get("errors") or []
    for err in errors:
        message = str((err or {}).get("message") or "").strip()
        if message:
            return message
    return ""


async def apply(db: AsyncSession, proposal: MaterializationProposal, *,
                mutate: Mutate, now: datetime) -> dict:
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

    async def create_issue(target_node_id: str, title: str, body: str, *,
                           parent: str | None) -> tuple[dict | None, str]:
        """Journal row → ``createIssue`` → mark. ``(issue, "")`` or ``(None, error)``."""
        mutation = Mutation("create_issue", target_node_id, None, None, title,
                            initiated_by)
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
            error = _error_text(payload) or "createIssue returned no issue"
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
                              _error_text(payload) or f"cannot resolve {repo}")
        created["repository_id"] = repository_id
        await remember()

    # --- the epic ----------------------------------------------------------- #
    epic: dict = dict(created.get("epic") or {})
    if not epic.get("node_id"):
        issue, error = await create_issue(
            repository_id, decomposition.epic_title, decomposition.epic_body,
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
        issue, error = await create_issue(str(epic["node_id"]), task.title, task.body,
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
    pending = [m for m in mutations if m.key() not in already]
    failures: list[str] = []
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
            error = f"{journal.TRANSIENT_PREFIX}{e}"
            for rid in row_ids:
                await db.run_sync(lambda s: journal.mark_failed(s, rid, error, now))
            failures.append(str(e))
            await db.commit()
            break
        result = parse_batch(payload, alias_map)
        for alias, rid in zip(alias_map, row_ids):
            error = result.errors.get(alias) or ""
            if error:
                failures.append(f"{alias}: {error}")
                await db.run_sync(lambda s: journal.mark_failed(s, rid, error, now))
            else:
                await db.run_sync(lambda s: journal.mark_applied(s, [rid], now))
        await db.commit()
    if failures:
        return await fail("edges", "; ".join(failures)[:500])

    # --- ccswitch-side overlay ---------------------------------------------- #
    ini.epic_repo, ini.epic_number = repo, epic.get("number")
    # Back to active: with an epic present the plan stage's approval gate applies
    # again (server/sdlc/engine.next_action), which is what the human advances.
    ini.status = "active"
    # Written as an upsert on (repo, number, kind), the table's own identity: a
    # resumed run — or a pm-sync pass that mirrored the new issues between the
    # failure and the retry — must not hit the unique constraint, and a mirrored
    # row still needs the classified overlay it would otherwise never get.
    numbers = [n.get("number") for n in task_nodes.values() if n.get("number")]
    by_number = {w.number: w for w in (await db.execute(select(WorkItem).where(
        WorkItem.repo == repo, WorkItem.kind == "issue",
        WorkItem.number.in_(numbers)))).scalars().all()}
    for idx, task in enumerate(decomposition.tasks):
        node = task_nodes[str(idx)]
        item = by_number.get(node.get("number"))
        if item is None:
            item = WorkItem(repo=repo, number=node.get("number"), kind="issue")
            db.add(item)
        item.state, item.title, item.body = "open", task.title, task.body
        item.url, item.gh_node_id = node.get("url"), node.get("node_id")
        # Classified by construction: these issues came out of a plan a human
        # approved, so they are queue-eligible without a triage pass. RICE is
        # left unset — Initiative carries no RICE columns to seed it from (spec
        # §7b's "score seeded from initiative RICE" has no source yet), and the
        # queue treats a missing score as unranked rather than inventing one.
        item.triage_state, item.classified_by = "classified", "materialize"
        item.classified_at = now
    proposal.status = "accepted"
    proposal.resolved_at = now
    sync_requested = await _request_sync(db, ini, repo, now)
    await db.commit()
    log.info("materialize.apply slug=%s epic=%s tasks=%d edges=%d",
             slug, epic.get("number"), len(decomposition.tasks), len(edges))
    return {"ok": True, "epic": epic, "tasks": len(decomposition.tasks),
            "edges": len(edges), "syncRequested": sync_requested}


async def _request_sync(db: AsyncSession, ini: Initiative, repo: str,
                        now: datetime) -> bool:
    """Ask pm-sync for a pass over the owning product, so the reconciler picks the
    new issues up. ``Initiative.project_id`` is nullable, so fall back to the
    repo's owning product; a repo no product owns is skipped, not guessed at."""
    project_id = ini.project_id
    if not project_id:
        owner = (await owning_projects_by_repo(db, [repo])).get(repo)
        project_id = owner.project_id if owner else None
    if not project_id:
        log.warning("materialize.apply: %s is owned by no product — board "
                    "membership will not be requested", repo)
        return False
    await request_project_sync(db, project_id, now)
    return True
