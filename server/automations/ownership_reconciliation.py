"""Conservative migration guard over existing work, before canonical acquisition.

This does not adopt or rewrite legacy history. All new actors must acquire the
ownership store after this guard; its transaction remains the concurrency fence.
"""
from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select

from server.automations.backlog_runner import is_in_flight
from server.automations.consolidation_engine import parse_consolidation_target
from server.automations.ownership import CanonicalTarget, OwnershipConflict
from server.automations.target_identity import TargetIdentityError, resolve_targets
from server.db import db_session
from server.db.models import (
    INACTIVE_SESSION_STATUSES, AutomationTargetState, Project, ProjectRepo, Session,
)
from server.pm.refs import split_ref


async def reconcile_legacy_targets(targets: Sequence[CanonicalTarget]) -> None:
    """Refuse verified overlap or unverifiable possibly active legacy work.

    Resolve aliases and every secondary batch/consolidation ref using the same
    authenticated transport as new claims. A missing session does not discharge
    an old claim. Terminal history without a live claim is not itself ownership.
    No writes: provenance, attempts, and outcomes are preserved.
    """
    if not targets:
        return
    async with db_session() as db:
        states = (await db.scalars(select(AutomationTargetState))).all()
        sessions = (await db.scalars(select(Session).where(
            Session.origin_ref.is_not(None),
            Session.status.not_in(INACTIVE_SESSION_STATUSES),
        ))).all()
        bindings = (await db.execute(select(Project.slug, ProjectRepo.repo).join(
            ProjectRepo, ProjectRepo.project_id == Project.id).where(
                ProjectRepo.watermark.is_not(None)))).all()
    # PM sync canonicalizes these bindings through mirror_store.rename_repo;
    # unsynced user-entered spellings cannot establish a disjoint scope.
    product_repos: dict[str, set[str]] = {}
    for slug, repo in bindings:
        if slug and repo:
            product_repos.setdefault(slug, set()).add(repo.lower())
    wanted_repos = {target.display_ref.rpartition("#")[0].lower() for target in targets}
    known_repos = set().union(*product_repos.values()) if product_repos else set()

    def unrelated_scope(product: object, repo: object, refs: object) -> bool:
        # A matching explicit ref always wins over stale product metadata. Scope
        # evidence only contains failures; successful alias resolution still wins.
        if isinstance(refs, list) and any(
            isinstance(ref, str) and ref.rpartition("#")[0].lower() in wanted_repos
            for ref in refs
        ):
            return False
        scope = product_repos.get(product, set()) if isinstance(product, str) else set()
        if isinstance(repo, str) and repo.lower() in known_repos:
            scope = scope | {repo.lower()}
        return bool(scope and wanted_repos <= known_repos and scope.isdisjoint(wanted_repos))

    # Include the source in a refusal so humans can find the old actor. Do not
    # key exclusion by mutable source/ref spellings.
    candidates: list[tuple[list[str], str, str, bool]] = []
    for row in states:
        state = row.state
        if state.get("ownership_token"):
            continue  # canonical acquisition checks these, including uncertainty
        if row.automation_id == "backlog-runner":
            active = is_in_flight(state) and not state.get("proposed")
        else:
            active = bool(state.get("claimed") or state.get("session_id"))
        if not active:
            continue
        refs = state.get("issues") or [row.target]
        scope_refs = [row.target, *(refs if isinstance(refs, list) else [])]
        unrelated = unrelated_scope(state.get("product"), state.get("repo"), scope_refs)
        if not isinstance(refs, list) or not all(isinstance(ref, str) for ref in refs):
            if unrelated:
                continue
            raise OwnershipConflict("legacy target references need reconciliation")
        candidates.append((refs, "pr" if row.automation_id == "pr-doctor" else "issue",
                           f"{row.automation_id}:{row.target}", unrelated))
    from server.automations.ownership import session_execution
    for session in sessions:
        association = await session_execution(session.id)
        if (association is not None and association[1] is not None
                and association[1] == association[2]):
            # Durable native proof, coupled with resume fencing, discharges this
            # exact managed actor. A label/status/display ref cannot do so.
            continue
        ref = session.origin_ref or ""
        unrelated = unrelated_scope(None, session.repo, [ref.removeprefix("consolidate:")])
        consolidation = parse_consolidation_target(ref)
        if consolidation is not None:
            repo, numbers = consolidation
            candidates.append(([f"{repo}#{number}" for number in numbers], "pr", session.id, unrelated))
        elif ref.startswith("consolidate:"):
            if unrelated:
                continue
            raise OwnershipConflict(f"legacy consolidation needs reconciliation: {session.id}")
        elif split_ref(ref) is not None:
            candidates.append(([ref], "pr" if session.origin == "pr-doctor" else "issue",
                               session.id, unrelated))
    wanted = {target.key for target in targets}
    for refs, kind, source, unrelated in candidates:
        try:
            resolved = (await resolve_targets(refs, kind="pr", include_linked_issues=True)
                        if kind == "pr" else await resolve_targets(refs, kind="issue"))
        except TargetIdentityError:
            if unrelated:
                continue
            raise
        if wanted.intersection(target.key for target in resolved):
            raise OwnershipConflict(f"legacy work needs reconciliation: {source}")
