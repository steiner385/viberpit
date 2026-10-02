"""Tests for Task 2: orchestrator.rehydrate() rebuilds detached SessionRecords."""
import asyncio
from server.db import init_db, db_session
from server.db.models import Session as DbSession
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory


async def _seed(rows):
    async with db_session() as db:
        for r in rows:
            db.add(DbSession(**r))


def test_rehydrate_loads_non_ended_rows_as_detached(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path/'ccswitch.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed([
            dict(id="a", name="Alpha", account_name="acct1",
                 engine_session_id="claude-a", status="idle", cwd="/repo/a"),
            dict(id="b", name="Beta", account_name="acct2",
                 engine_session_id=None, status="working", cwd="/repo/b"),
            dict(id="c", name="Gone", account_name="acct1",
                 engine_session_id="claude-c", status="ended", cwd="/repo/c"),
        ])
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        n = await orch.rehydrate()
        return n, {r.id: r for r in orch.list_sessions()}, orch._drivers

    n, recs, drivers = asyncio.run(run())
    assert n == 2                       # a + b, not the ended c
    assert set(recs) == {"a", "b"}
    assert recs["a"].detached is True
    assert recs["a"].engine_session_id == "claude-a"
    assert recs["b"].engine_session_id == ""   # NULL coerced to ""
    assert recs["b"].status == "idle"          # working normalized to idle
    assert drivers == {}                        # lazy: no drivers spawned


def test_rehydrate_normalizes_awaiting_human_statuses(tmp_path):
    """A restart wipes the in-memory permission/question registries, so a row
    persisted as waiting_input / waiting_question / ready_review can never re-derive
    that status — it would keep a dead "needs you" badge with no prompt to answer.
    rehydrate() must normalize all live/in-memory-derived statuses to idle
    (resumable) and flag them interrupted_by_restart, exactly like mid-turn rows."""
    db_url = f"sqlite+aiosqlite:///{tmp_path/'ccswitch.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed([
            dict(id="perm", name="Perm", account_name="x",
                 engine_session_id="cs1", status="waiting_input", cwd="/r"),
            dict(id="ques", name="Ques", account_name="x",
                 engine_session_id="cs2", status="waiting_question", cwd="/r"),
            dict(id="plan", name="Plan", account_name="x",
                 engine_session_id="cs3", status="ready_review", cwd="/r"),
            dict(id="calm", name="Calm", account_name="x",
                 engine_session_id="cs4", status="idle", cwd="/r"),
        ])
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        await orch.rehydrate()
        return {r.id: r for r in orch.list_sessions()}

    recs = asyncio.run(run())
    for sid in ("perm", "ques", "plan"):
        assert recs[sid].status == "idle", f"{sid} not normalized"
        assert recs[sid].interrupted_by_restart is True, f"{sid} missing restart flag"
    # A genuinely-idle row is untouched — not spuriously flagged interrupted.
    assert recs["calm"].status == "idle"
    assert recs["calm"].interrupted_by_restart is False


def test_rehydrate_persists_waiting_input_normalization_to_db(tmp_path):
    """Companion to #785 (issue #789): when a waiting_input session has no live
    process (pid=None), rehydrate() must also persist the idle normalization to the
    DB row — not just the in-memory record. Without the DB write the row stays
    waiting_input forever (the next _persist_status call never comes for an idle
    session that never runs again), causing a dead badge across restarts."""
    db_url = f"sqlite+aiosqlite:///{tmp_path/'ccswitch.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed([
            dict(id="orphan", name="Orphan", account_name="x",
                 engine_session_id="cs1", status="waiting_input", cwd="/r",
                 pid=None),
        ])
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        await orch.rehydrate()
        # Read back from the DB directly — not from the in-memory record.
        async with db_session() as db:
            from sqlalchemy import select as sa_select
            row = (await db.execute(
                sa_select(DbSession).where(DbSession.id == "orphan")
            )).scalar_one()
            return row.status

    db_status = asyncio.run(run())
    assert db_status == "idle", (
        "waiting_input orphan should be persisted as idle in DB on restart"
    )


def test_rehydrate_is_idempotent(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path/'ccswitch.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed([dict(id="a", name="A", account_name="x",
                          engine_session_id="cs", status="idle", cwd="/r")])
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        first = await orch.rehydrate()
        second = await orch.rehydrate()    # no new rows added
        return first, second, len(orch.list_sessions())

    first, second, count = asyncio.run(run())
    assert (first, second, count) == (1, 0, 1)


def test_rehydrate_keeps_the_hand_back_ask_and_its_suspected_complete_verdict(tmp_path):
    """Prod 2026-10-02 (session 6ab5c602 on rocky): a deploy restarted the daemon while
    the session sat at `handed_back`. The row still held the reason the session gave
    `hand_back(reason)`, but the rebuilt in-memory record did not — `_record_from_row`
    never mapped the column — so the edge serializer (which reads the record, not the
    row) sent `handBackReason: null` and both the card and the banner fell back to
    "the session stopped and is waiting on you". The reason named four of the five
    options the user was then told were "above". Same for the judge's verdict beside
    it: both columns only mean anything with status == handed_back, which survives a
    restart (it is a resting status), so both must survive with it."""
    db_url = f"sqlite+aiosqlite:///{tmp_path/'viberpit.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed([
            dict(id="hb", name="Plan", account_name="acct1", engine_session_id="claude-hb",
                 status="handed_back", cwd="/repo/hb",
                 hand_back_reason="Pick the next piece of work: A, B, C or D.",
                 suspected_complete_reason="PR #239 merged; nothing left to do"),
        ])
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        await orch.rehydrate()
        return orch.record("hb")

    rec = asyncio.run(run())
    assert rec.status == "handed_back"
    assert rec.hand_back_reason == "Pick the next piece of work: A, B, C or D."
    assert rec.suspected_complete_reason == "PR #239 merged; nothing left to do"
    # The daemon→edge RPC copy is what a remote host's card is drawn from.
    assert rec.to_dict()["handBackReason"] == "Pick the next piece of work: A, B, C or D."
