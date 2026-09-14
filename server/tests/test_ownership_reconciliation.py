"""Migration guard resolves legacy actor targets before allowing new work."""
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from server.automations import ownership_reconciliation as guard
from server.automations.ownership import CanonicalTarget, OwnershipConflict
from server.automations.target_identity import TargetIdentityError
from server.db import db_session
from server.db.models import Automation, AutomationTargetState, Session

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("isolated_db")]


async def legacy_state(state, *, actor="backlog-runner", ref="old/repo#1"):
    async with db_session() as db:
        db.add(Automation(id=actor))
        await db.flush()
        db.add(AutomationTargetState(automation_id=actor, target=ref, state=state))
        await db.commit()


async def legacy_session(ref, *, origin="other-automation", status="working"):
    async with db_session() as db:
        db.add(Session(id="legacy", name="legacy", account_name="test", cwd="/tmp",
                       origin_ref=ref, origin=origin, status=status))
        await db.commit()


@pytest.mark.parametrize("state", [
    {"claimed": True, "issues": ["old/repo#1", "old/repo#2"]},
    {"session_id": "missing", "issues": ["old/repo#1", "old/repo#2"]},
])
async def test_secondary_alias_overlap_refused_without_rewriting_history(monkeypatch, state):
    await legacy_state(state)
    resolver = AsyncMock(return_value=(CanonicalTarget("I_secondary", "new/repo#2"),))
    monkeypatch.setattr(guard, "resolve_targets", resolver)
    with pytest.raises(OwnershipConflict, match="backlog-runner"):
        await guard.reconcile_legacy_targets([CanonicalTarget("I_secondary", "new/repo#2")])
    resolver.assert_awaited_once_with(["old/repo#1", "old/repo#2"], kind="issue")
    async with db_session() as db:
        assert (await db.scalar(select(AutomationTargetState))).state == state


@pytest.mark.parametrize("state", [{"gave_up": True}, {"handed_off": True}, {"proposed": True}])
async def test_inactive_runner_history_does_not_claim_targets(monkeypatch, state):
    await legacy_state(state)
    resolver = AsyncMock()
    monkeypatch.setattr(guard, "resolve_targets", resolver)
    await guard.reconcile_legacy_targets([CanonicalTarget("I_one", "new/repo#1")])
    resolver.assert_not_awaited()


@pytest.mark.parametrize("ref,origin,refs,kind", [
    ("old/repo#2", "other-automation", ["old/repo#2"], "issue"),
    ("consolidate:old/repo#2+3", "pr-doctor", ["old/repo#2", "old/repo#3"], "pr"),
])
async def test_cross_origin_and_partial_consolidation_overlap(monkeypatch, ref, origin, refs, kind):
    await legacy_session(ref, origin=origin)
    resolver = AsyncMock(return_value=(CanonicalTarget("NODE", "new/repo#2"),))
    monkeypatch.setattr(guard, "resolve_targets", resolver)
    with pytest.raises(OwnershipConflict, match="legacy"):
        await guard.reconcile_legacy_targets([CanonicalTarget("NODE", "new/repo#2")])
    if kind == "pr":
        resolver.assert_awaited_once_with(refs, kind=kind, include_linked_issues=True)
    else:
        resolver.assert_awaited_once_with(refs, kind=kind)


async def test_unavailable_identity_never_permits_new_claim(monkeypatch):
    await legacy_state({"claimed": True})
    monkeypatch.setattr(guard, "resolve_targets", AsyncMock(side_effect=TargetIdentityError("offline")))
    with pytest.raises(TargetIdentityError):
        await guard.reconcile_legacy_targets([CanonicalTarget("NODE", "new/repo#1")])


async def test_unrelated_verified_work_allowed(monkeypatch):
    await legacy_session("other/repo#1")
    monkeypatch.setattr(guard, "resolve_targets", AsyncMock(
        return_value=(CanonicalTarget("OTHER", "other/repo#1"),)))
    await guard.reconcile_legacy_targets([CanonicalTarget("NODE", "new/repo#1")])


async def test_terminal_history_without_claim_is_not_live_ownership(monkeypatch):
    await legacy_session("old/repo#1", status="archived")
    resolver = AsyncMock()
    monkeypatch.setattr(guard, "resolve_targets", resolver)
    await guard.reconcile_legacy_targets([CanonicalTarget("NODE", "new/repo#1")])
    resolver.assert_not_awaited()


async def product_scopes():
    from datetime import datetime
    from server.db.models import Project, ProjectRepo
    async with db_session() as db:
        for slug, repo in (("healthy", "new/repo"), ("unrelated", "other/repo")):
            db.add(Project(id=slug, slug=slug))
            await db.flush()
            db.add(ProjectRepo(project_id=slug, repo=repo, watermark=datetime(2026, 9, 14)))
        await db.commit()


@pytest.mark.parametrize("product,blocked", [("healthy", True), ("unrelated", False), (None, True)])
async def test_failed_identity_is_contained_to_known_product_scope(monkeypatch, product, blocked):
    await product_scopes()
    await legacy_state({"claimed": True, "product": product}, ref="alias/repo#1")
    resolver = AsyncMock(side_effect=TargetIdentityError("inaccessible repository"))
    monkeypatch.setattr(guard, "resolve_targets", resolver)
    if blocked:
        with pytest.raises(TargetIdentityError):
            await guard.reconcile_legacy_targets([CanonicalTarget("NODE", "new/repo#1")])
    else:
        await guard.reconcile_legacy_targets([CanonicalTarget("NODE", "new/repo#1")])
    resolver.assert_awaited_once()


@pytest.mark.parametrize("product,blocked", [("healthy", True), ("unrelated", False)])
async def test_malformed_batch_does_not_freeze_other_product(monkeypatch, product, blocked):
    await product_scopes()
    await legacy_state({"claimed": True, "product": product, "issues": [42]})
    if blocked:
        with pytest.raises(OwnershipConflict):
            await guard.reconcile_legacy_targets([CanonicalTarget("NODE", "new/repo#1")])
    else:
        await guard.reconcile_legacy_targets([CanonicalTarget("NODE", "new/repo#1")])


async def test_verified_alias_overlap_wins_over_different_product_metadata(monkeypatch):
    await product_scopes()
    await legacy_state({"claimed": True, "product": "unrelated"})
    monkeypatch.setattr(guard, "resolve_targets", AsyncMock(
        return_value=(CanonicalTarget("NODE", "new/repo#1"),)))
    with pytest.raises(OwnershipConflict):
        await guard.reconcile_legacy_targets([CanonicalTarget("NODE", "new/repo#1")])


@pytest.mark.parametrize("repo,blocked", [("new/repo", True), ("other/repo", False)])
async def test_malformed_consolidation_scoped_by_session_repository(repo, blocked):
    await product_scopes()
    await legacy_session("consolidate:alias/repo#bad", origin="pr-doctor")
    async with db_session() as db:
        session = await db.get(Session, "legacy")
        session.repo = repo
        await db.commit()
    if blocked:
        with pytest.raises(OwnershipConflict):
            await guard.reconcile_legacy_targets([CanonicalTarget("NODE", "new/repo#1")])
    else:
        await guard.reconcile_legacy_targets([CanonicalTarget("NODE", "new/repo#1")])


async def test_primary_ref_blocks_despite_malformed_batch_and_stale_product():
    await product_scopes()
    await legacy_state({"claimed": True, "product": "unrelated", "issues": [42]},
                       ref="new/repo#1")
    with pytest.raises(OwnershipConflict):
        await guard.reconcile_legacy_targets([CanonicalTarget("NODE", "new/repo#1")])


@pytest.mark.parametrize("ref", ["old/repo#2", "consolidate:old/repo#2+3"])
async def test_legacy_pr_work_excludes_verified_linked_issue(monkeypatch, ref):
    await legacy_session(ref, origin="pr-doctor")

    async def resolve(refs, *, kind, include_linked_issues=False):
        assert kind == "pr"
        targets = [CanonicalTarget("PR_two", "old/repo#2")]
        if include_linked_issues:
            targets.append(CanonicalTarget("I_one", "different/issues#1"))
        return tuple(targets)

    monkeypatch.setattr(guard, "resolve_targets", resolve)
    with pytest.raises(OwnershipConflict, match="legacy"):
        await guard.reconcile_legacy_targets([CanonicalTarget("I_one", "different/issues#1")])
