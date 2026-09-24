"""Archive Audit review findings (Phase 4, #1611): durable, deduped, never resurrected."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from sqlalchemy import select

from server.automations.action_providers import ArchiveAuditActionProvider
from server.automations.archive_audit import persist_findings
from server.db import db_session
from server.db.models import ArchiveReviewFinding


def _r(sid, bucket="premature", reason="open PR, not armed", repo="o/r"):
    return {"id": sid, "bucket": bucket, "reason": reason, "goal": f"goal {sid}",
            "action": "none", "repo": repo}


async def _rows():
    async with db_session() as db:
        return {r.session_id: r for r in (await db.execute(select(ArchiveReviewFinding))).scalars()}


def test_propose_opens_one_finding_and_returns_nothing_to_unarchive(isolated_db):
    async def run():
        out = await persist_findings(db_session, [_r("s1"), _r("s2", bucket="legit")], "propose")
        assert out["to_unarchive"] == [] and out["opened"] == 1 and out["open_total"] == 1
        rows = await _rows()
        assert set(rows) == {"s1"} and rows["s1"].status == "open"
        assert rows["s1"].reason == "open PR, not armed" and rows["s1"].repo == "o/r"
        # Same unchanged finding on the next tick: refreshed, NOT duplicated.
        again = await persist_findings(db_session, [_r("s1")], "propose")
        assert again["opened"] == 0 and again["open_total"] == 1
        assert len(await _rows()) == 1
    asyncio.run(run())


def test_a_retained_finding_is_never_resurrected(isolated_db):
    """The human's decision must survive every later tick with the same evidence."""
    async def run():
        await persist_findings(db_session, [_r("s1")], "propose")
        async with db_session() as db:
            row = (await db.execute(select(ArchiveReviewFinding))).scalar_one()
            row.status = "retained"
            await db.commit()
        for mode in ("propose", "auto_reopen"):
            out = await persist_findings(db_session, [_r("s1")], mode)
            # Not reopened even under the opt-in mode: reopening would undo the decision.
            assert out == {"to_unarchive": [], "opened": 0, "resolved": 0,
                           "reopened": 0, "open_total": 0}
        assert (await _rows())["s1"].status == "retained"
    asyncio.run(run())


def test_auto_reopen_records_the_decision_and_returns_the_session(isolated_db):
    async def run():
        out = await persist_findings(db_session, [_r("s1")], "auto_reopen")
        assert out["to_unarchive"] == ["s1"] and out["reopened"] == 1 and out["open_total"] == 0
        row = (await _rows())["s1"]
        assert row.status == "reopened" and row.decided_at is not None
        assert "auto_reopen" in row.decided_by
        # Idempotent: a session already reopened is not returned for another unarchive.
        assert (await persist_findings(db_session, [_r("s1")], "auto_reopen"))["to_unarchive"] == []
    asyncio.run(run())


def test_an_open_finding_self_resolves_when_the_session_is_no_longer_premature(isolated_db):
    async def run():
        await persist_findings(db_session, [_r("s1"), _r("s2")], "propose")
        out = await persist_findings(db_session, [_r("s1", bucket="legit")], "propose")
        assert out["resolved"] == 2 and out["open_total"] == 0
        assert {r.status for r in (await _rows()).values()} == {"resolved"}
    asyncio.run(run())


class _Orch:
    """Just the two seams the provider calls, backed by the real orchestrator methods."""
    def __init__(self, unarchive_result):
        from server.sessions.orchestrator import SessionOrchestrator
        self.unarchived: list[str] = []
        self._result = unarchive_result
        self.archive_audit_open_findings = (
            lambda: SessionOrchestrator.archive_audit_open_findings(self))
        self.archive_audit_decide_finding = (
            lambda fid, d: SessionOrchestrator.archive_audit_decide_finding(self, fid, d))

    async def unarchive(self, sid):
        self.unarchived.append(sid)
        return SimpleNamespace(id=sid) if self._result else None


def test_provider_lists_open_findings_with_retain_and_reopen_and_decides_once(isolated_db):
    async def run():
        await persist_findings(db_session, [_r("s1")], "propose")
        orch = _Orch(unarchive_result=True)
        provider = ArchiveAuditActionProvider(orch)
        items = await provider.actionable()
        assert [a.kind for a in items[0].actions] == ["retain", "reopen"]
        assert items[0].actions[1].style == "risky"   # reopen is the consequential one

        res = await provider.trigger(items[0].id, "reopen")
        assert res.status == "reopened" and res.level == "ok"
        assert orch.unarchived == ["s1"]
        assert (await _rows())["s1"].status == "reopened"
        assert await provider.actionable() == []
        # A second click (double-tap / two operators) is refused, not re-applied.
        assert (await provider.trigger(items[0].id, "retain")).status == "already_decided"
        assert orch.unarchived == ["s1"]
    asyncio.run(run())


def test_a_failed_reopen_leaves_the_finding_open_for_retry(isolated_db):
    async def run():
        await persist_findings(db_session, [_r("s1")], "propose")
        provider = ArchiveAuditActionProvider(_Orch(unarchive_result=False))
        item = (await provider.actionable())[0]
        res = await provider.trigger(item.id, "reopen")
        assert res.status == "unarchive_failed" and res.level == "error"
        assert (await _rows())["s1"].status == "open"
    asyncio.run(run())


def test_retain_keeps_the_session_archived(isolated_db):
    async def run():
        await persist_findings(db_session, [_r("s1")], "propose")
        orch = _Orch(unarchive_result=True)
        provider = ArchiveAuditActionProvider(orch)
        item = (await provider.actionable())[0]
        assert (await provider.trigger(item.id, "retain")).status == "retained"
        assert orch.unarchived == []
        assert (await _rows())["s1"].status == "retained"
    asyncio.run(run())
