import pytest
from sqlalchemy import select

from server.automations.ownership import (
    CanonicalTarget, OwnershipConflict, ReleaseProof, StaleOwnership, acquire, owned,
)
from server.automations.ownership_handoff import expanded_handoff
from server.db.models import AutomationOperation
from server.tests.test_automation_ownership import ownership_db as ownership_db


@pytest.mark.asyncio
async def test_expanded_transfer_preserves_history_and_excludes_overlap(ownership_db):
    issue = CanonicalTarget("I_one", "owner/repo#1")
    pr = CanonicalTarget("PR_two", "owner/repo#2")
    async with acquire([issue], owner="runner", source="auto", reason="implementation") as tx:
        old = tx.token
    async with expanded_handoff(old, [pr]) as tx:
        new = await tx.transfer(owner="pr-doctor", source="handoff", reason="ready",
                                proof=ReleaseProof.NOT_STARTED, successor_targets=[pr])
    async with ownership_db() as db:
        ops = {o.token: o for o in (await db.scalars(select(AutomationOperation))).all()}
        assert ops[old].targets == [issue.key]
        assert ops[new].targets == sorted([issue.key, pr.key])
        assert ops[new].predecessor_token == old
    with pytest.raises(StaleOwnership):
        async with owned(old):
            pass
    with pytest.raises(OwnershipConflict):
        async with acquire([pr], owner="manual", source="manual", reason="repair"):
            pass


@pytest.mark.asyncio
async def test_failed_expansion_keeps_original_owner_and_unclaimed_extra(ownership_db):
    issue = CanonicalTarget("I_one", "owner/repo#1")
    pr = CanonicalTarget("PR_two", "owner/repo#2")
    async with acquire([issue], owner="runner", source="auto", reason="implementation") as tx:
        old = tx.token
        tx.reserve_session()
    with pytest.raises(ValueError, match="stopped-work"):
        async with expanded_handoff(old, [pr]) as tx:
            await tx.transfer(owner="pr-doctor", source="handoff", reason="ready",
                              proof=ReleaseProof.WORK_STOPPED, successor_targets=[pr])
    async with owned(old) as tx:
        assert tx.operation.phase == "launching"
    async with acquire([pr], owner="manual", source="manual", reason="repair"):
        pass
