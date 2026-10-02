"""SessionRecord carries background_work (and sub_agents) across the daemon→edge RPC
(#2635). sub_agents was never in to_dict/from_dict, so fleet-mode cards showed no
sub-agent rows — fixed in the same hunk."""
import asyncio

from sqlalchemy import select as sa_select

from server.db import db_session, init_db
from server.db.models import Session as DbSession
from server.sessions.background_tasks import EMPTY_BACKGROUND_WORK
from server.sessions.engine import AgentEngine
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.types import SessionRecord
from server.tests.fakes.fake_factory import fake_sdk_factory


def _rec(**over):
    base = dict(id="s1", name="n", account="a", engine=AgentEngine.CLAUDE_CODE,
                engine_session_id="c1", cols=80, rows=24, cwd="/tmp", status="done")
    base.update(over)
    return SessionRecord(**base)


def test_background_work_and_sub_agents_round_trip():
    snap = {"items": [{"toolUseId": "tu1", "kind": "task", "name": "Bash",
                       "description": "pytest", "startedAt": None}],
            "wakeup": False, "wakeupAt": None}
    agents = [{"agentId": "tu9", "parentAgentId": None, "agentType": "Explore",
               "description": "d", "status": "running"}]
    d = _rec(background_work=snap, sub_agents=agents).to_dict()
    assert d["backgroundWork"] == snap and d["subAgents"] == agents
    back = SessionRecord.from_dict(d)
    assert back.background_work == snap and back.sub_agents == agents


def test_from_dict_tolerates_an_older_daemon_without_the_keys():
    d = _rec().to_dict()
    d.pop("backgroundWork")
    d.pop("subAgents")
    back = SessionRecord.from_dict(d)
    assert back.background_work is None and back.sub_agents is None


SNAP = {"items": [{"toolUseId": "tu1", "kind": "task", "name": "Bash",
                   "description": "pytest", "startedAt": None}],
        "wakeup": False, "wakeupAt": None}


async def _orch(tmp_path, *, status="idle", background_work=None):
    await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'viberpit.db'}")
    async with db_session() as db:
        db.add(DbSession(id="s1", name="n", account_name="acct1",
                         engine_session_id="claude-s1", status=status, cwd="/repo/s1",
                         background_work=background_work))
    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
    await orch.rehydrate()
    return orch


async def _row():
    async with db_session() as db:
        return (await db.execute(sa_select(DbSession).where(DbSession.id == "s1"))).scalars().first()


def _patch_parked(orch, parked):
    async def is_parked(sid):
        return parked

    async def snapshot(sid):
        return SNAP

    async def prose(sid):
        return ""

    async def awaits(sid, text):
        return False
    orch._session_is_parked = is_parked
    orch._background_work_snapshot = snapshot
    orch._last_assistant_prose = prose
    orch._parked_prose_awaits_user = awaits


def test_finished_parked_turn_persists_the_snapshot_to_record_and_row(tmp_path):
    async def run():
        orch = await _orch(tmp_path)
        _patch_parked(orch, True)
        final = await orch._persist_status("s1", "done")
        return final, orch.record("s1"), await _row()
    final, rec, row = asyncio.run(run())
    assert final == "waiting_external"
    assert rec.background_work == SNAP and row.background_work == SNAP


def test_finished_unparked_turn_clears_the_snapshot(tmp_path):
    async def run():
        orch = await _orch(tmp_path, background_work=SNAP)
        orch.record("s1").background_work = SNAP
        _patch_parked(orch, False)
        final = await orch._persist_status("s1", "done")
        return final, orch.record("s1"), await _row()
    final, rec, row = asyncio.run(run())
    assert final == "done"
    assert rec.background_work is None and row.background_work is None


def test_a_non_finished_status_leaves_the_stored_snapshot_alone(tmp_path):
    async def run():
        orch = await _orch(tmp_path, background_work=SNAP)
        orch.record("s1").background_work = SNAP
        _patch_parked(orch, False)
        await orch._persist_status("s1", "in_progress")
        return orch.record("s1"), await _row()
    rec, row = asyncio.run(run())
    assert rec.background_work == SNAP and row.background_work == SNAP


def test_rehydrate_unpark_heal_clears_the_snapshot(tmp_path):
    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'viberpit.db'}")
        async with db_session() as db:
            db.add(DbSession(id="s1", name="n", account_name="acct1",
                             engine_session_id="claude-s1", status="waiting_external",
                             cwd="/repo/s1", background_work=SNAP))
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())

        async def not_parked(sid):
            return False
        orch._session_is_parked = not_parked
        await orch.rehydrate()
        return orch.record("s1"), await _row()
    rec, row = asyncio.run(run())
    assert rec.status == "done" and row.status == "done"
    assert rec.background_work is None and row.background_work is None


def test_snapshot_failure_degrades_to_nothing_open(tmp_path):
    async def run():
        orch = await _orch(tmp_path)

        def boom(sid):
            raise RuntimeError("no transcript")
        orch.transcript_path = boom
        return await orch._background_work_snapshot("s1")
    assert asyncio.run(run()) == EMPTY_BACKGROUND_WORK
