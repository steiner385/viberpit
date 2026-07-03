"""Tests for orchestrator status persistence + Claude auto-naming."""
import asyncio
import types

from server.db import init_db, db_session
from server.db.models import Session as DbSession
from server.sessions import events as ev
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory


async def _seed(rows):
    async with db_session() as db:
        for r in rows:
            db.add(DbSession(**r))


async def _build_orch(tmp_path, **row):
    db_url = f"sqlite+aiosqlite:///{tmp_path/'ccswitch.db'}"
    await init_db(db_url=db_url)
    base = dict(id="s1", name="New Session", account_name="acct1",
                claude_session_id="claude-s1", status="idle", cwd="/repo/s1")
    base.update(row)
    await _seed([base])
    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
    await orch.rehydrate()
    return orch


def _attach_stub_driver(orch, sid):
    """Give a (detached) session a live-driver stand-in whose emit() feeds a drainable
    queue, mirroring the real driver's broadcast emit path (orchestrator calls
    driver.emit(...) for out-of-band events like a rename Title)."""
    q: asyncio.Queue = asyncio.Queue()
    drv = types.SimpleNamespace(_events=q, emit=q.put_nowait)
    orch._drivers[sid] = drv
    return drv


def test_persist_status_updates_record(tmp_path):
    async def run():
        orch = await _build_orch(tmp_path)
        await orch._persist_status("s1", "waiting_input")
        return orch.record("s1").status
    assert asyncio.run(run()) == "waiting_input"


def test_persist_status_never_overwrites_ended(tmp_path):
    async def run():
        orch = await _build_orch(tmp_path)
        await orch._persist_status("s1", "ended")
        await orch._persist_status("s1", "in_progress")
        return orch.record("s1").status
    assert asyncio.run(run()) == "ended"


def test_persist_status_broadcasts_status_event(tmp_path):
    """A status change is published on the lifecycle event bus so every connected
    dashboard live-patches the session LIST immediately, instead of waiting for the
    (background-throttled) 5s poll. Without this push the list goes stale — e.g.
    stuck on 'Working…' — until the user opens the session."""
    async def run():
        orch = await _build_orch(tmp_path)
        q = orch.subscribe_events()
        await orch._persist_status("s1", "waiting_input")
        return q.get_nowait()
    event = asyncio.run(run())
    assert event["type"] == "status"
    assert event["sessionId"] == "s1"
    assert event["status"] == "waiting_input"
    assert "updatedAt" in event  # live-push now carries the authoritative freshness timestamp


def test_persist_status_ended_does_not_broadcast(tmp_path):
    """An already-ended session is terminal: no further status broadcasts."""
    async def run():
        orch = await _build_orch(tmp_path)
        await orch._persist_status("s1", "ended")
        q = orch.subscribe_events()
        await orch._persist_status("s1", "in_progress")  # ignored — terminal
        return q.empty()
    assert asyncio.run(run()) is True


def _parked_transcript(tmp_path):
    """A transcript whose last turn armed a ScheduleWakeup → the session is parked."""
    p = tmp_path / "claude-s1.jsonl"
    p.write_text(
        '{"type":"user","message":{"role":"user","content":"poll the PRs"}}\n'
        '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"t1",'
        '"name":"ScheduleWakeup","input":{"delaySeconds":1200,"reason":"watching CI"}}]}}\n'
    )
    return p


def test_persist_status_done_overlays_waiting_external_when_parked(tmp_path):
    """A finished turn that left an armed wakeup is "Waiting…", not "Your turn"."""
    async def run():
        orch = await _build_orch(tmp_path, name_is_auto=False)  # isolate from auto-naming
        orch.transcript_path = lambda sid: str(_parked_transcript(tmp_path))
        await orch._persist_status("s1", "done")
        return orch.record("s1").status
    assert asyncio.run(run()) == "waiting_external"


def _parked_asking_transcript(tmp_path):
    """Parked on an open background task AND the final assistant message hands a prose
    question/decision back to the user — the real "Waiting… masks needs-you" bug."""
    p = tmp_path / "claude-s1.jsonl"
    p.write_text(
        '{"type":"user","message":{"role":"user","content":"unblock the PRs"}}\n'
        '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"t1",'
        '"name":"Bash","input":{"command":"gh pr checks","run_in_background":true}}]}}\n'
        '{"type":"assistant","message":{"content":[{"type":"text","text":'
        '"Two execution options:\\n1. Subagent-Driven (recommended).\\n'
        '2. Inline Execution."}]}}\n'
    )
    return p


def test_persist_status_done_parked_but_asking_user_is_answer_needed(tmp_path):
    """A parked turn whose final message hands a prose question/decision back to the
    user must read "Answer needed" (needs-you), not be masked as "Waiting…"."""
    async def run():
        orch = await _build_orch(tmp_path, name_is_auto=False)
        orch.transcript_path = lambda sid: str(_parked_asking_transcript(tmp_path))
        await orch._persist_status("s1", "done")
        return orch.record("s1").status
    assert asyncio.run(run()) == "waiting_question"


def _killed_only_transcript(tmp_path):
    """A transcript whose only background poller was killed by a deploy → NOT parked
    (killed tasks are dead, re-armed under a new id), so is_parked() is False."""
    p = tmp_path / "claude-s1.jsonl"
    p.write_text(
        '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"t1",'
        '"name":"Bash","input":{"command":"gh pr view 1","run_in_background":true}}]}}\n'
        '{"type":"queue-operation","operation":"enqueue","content":'
        '"<task-notification>\\n<tool-use-id>t1</tool-use-id>\\n'
        '<status>killed</status>\\n</task-notification>"}\n'
    )
    return p


async def _seed_and_rehydrate(tmp_path, transcript_factory, **row):
    """Seed one row, override transcript_path, rehydrate, return (record_status, db_status)."""
    from sqlalchemy import select as sa_select
    db_url = f"sqlite+aiosqlite:///{tmp_path/'ccswitch.db'}"
    await init_db(db_url=db_url)
    base = dict(id="s1", name="x", account_name="a", claude_session_id="cs1",
                status="waiting_external", cwd="/repo/s1", name_is_auto=False)
    base.update(row)
    await _seed([base])
    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
    orch.transcript_path = lambda sid: str(transcript_factory(tmp_path))
    await orch.rehydrate()
    async with db_session() as db:
        db_status = (await db.execute(
            sa_select(DbSession.status).where(DbSession.id == "s1"))).scalar_one()
    return orch.record("s1").status, db_status


def test_rehydrate_heals_stuck_waiting_external_when_no_longer_parked(tmp_path):
    # The real bug: a deploy killed the pollers, the row is stuck on waiting_external.
    # Rehydrate (next daemon start) restores the underlying `done`.
    rec, db = asyncio.run(_seed_and_rehydrate(tmp_path, _killed_only_transcript))
    assert rec == "done"
    assert db == "done"


def test_rehydrate_keeps_waiting_external_when_still_parked(tmp_path):
    rec, db = asyncio.run(_seed_and_rehydrate(tmp_path, _parked_transcript))
    assert rec == "waiting_external"
    assert db == "waiting_external"


def test_persist_status_done_stays_done_when_not_parked(tmp_path):
    transcript = tmp_path / "claude-s1.jsonl"
    transcript.write_text('{"type":"assistant","message":{"content":[]}}\n')

    async def run():
        orch = await _build_orch(tmp_path)
        orch.transcript_path = lambda sid: str(transcript)
        await orch._persist_status("s1", "done")
        return orch.record("s1").status
    assert asyncio.run(run()) == "done"


def test_persist_status_waiting_external_does_not_advance_active_clock(tmp_path):
    """Parking is not a fresh "return": the 'active … ago' clock keeps the prior turn's
    time, so a parked card shows how long it has been waiting (since the last turn)
    rather than resetting to 'now' on every re-arm."""
    async def run():
        orch = await _build_orch(tmp_path, name_is_auto=False)
        orch.transcript_path = lambda sid: str(_parked_transcript(tmp_path))
        orch.record("s1").last_turn_at = None
        await orch._persist_status("s1", "done")  # parked → waiting_external
        return orch.record("s1").status, orch.record("s1").last_turn_at
    status, last_turn_at = asyncio.run(run())
    assert status == "waiting_external"
    assert last_turn_at is None  # the parked turn must NOT bump the clock


def test_persist_status_waiting_external_broadcasts_overlaid_status(tmp_path):
    """The live-push event carries the overlaid status, not the raw `done`."""
    async def run():
        orch = await _build_orch(tmp_path, name_is_auto=False)  # isolate from auto-naming
        orch.transcript_path = lambda sid: str(_parked_transcript(tmp_path))
        q = orch.subscribe_events()
        await orch._persist_status("s1", "done")
        return q.get_nowait()
    event = asyncio.run(run())
    assert event["type"] == "status"
    assert event["sessionId"] == "s1"
    assert event["status"] == "waiting_external"
    assert "updatedAt" in event  # live-push now carries the authoritative freshness timestamp


def test_rename_pins_name_is_auto_false(tmp_path):
    async def run():
        orch = await _build_orch(tmp_path)
        await orch.rename_session("s1", "My Name")
        rec = orch.record("s1")
        return rec.name, rec.name_is_auto
    name, is_auto = asyncio.run(run())
    assert name == "My Name"
    assert is_auto is False


def test_auto_name_applied_on_done(tmp_path):
    transcript = tmp_path / "claude-s1.jsonl"
    transcript.write_text('{"ai-title": "Fix the login bug"}\n')

    async def run():
        orch = await _build_orch(tmp_path)
        orch.transcript_path = lambda sid: str(transcript)
        await orch._persist_status("s1", "done")
        return orch.record("s1").name
    assert asyncio.run(run()) == "Fix the login bug"


def test_auto_name_not_applied_when_manual(tmp_path):
    transcript = tmp_path / "claude-s1.jsonl"
    transcript.write_text('{"ai-title": "Fix the login bug"}\n')

    async def run():
        orch = await _build_orch(tmp_path, name="Hand Picked", name_is_auto=False)
        orch.transcript_path = lambda sid: str(transcript)
        await orch._persist_status("s1", "done")
        return orch.record("s1").name
    assert asyncio.run(run()) == "Hand Picked"


def test_auto_name_emits_title_event_to_live_header(tmp_path):
    transcript = tmp_path / "claude-s1.jsonl"
    transcript.write_text('{"type":"ai-title","aiTitle":"Fix the login bug"}\n')

    async def run():
        orch = await _build_orch(tmp_path)
        orch.transcript_path = lambda sid: str(transcript)
        drv = _attach_stub_driver(orch, "s1")
        await orch._persist_status("s1", "done")
        events = []
        while not drv._events.empty():
            events.append(drv._events.get_nowait())
        return orch.record("s1").name, events

    name, events = asyncio.run(run())
    assert name == "Fix the login bug"
    titles = [e for e in events if isinstance(e, ev.Title)]
    assert len(titles) == 1
    assert titles[0].title == "Fix the login bug"
    assert titles[0].source == "claude"


def test_auto_name_idempotent_no_event_when_unchanged(tmp_path):
    transcript = tmp_path / "claude-s1.jsonl"
    transcript.write_text('{"type":"ai-title","aiTitle":"Fix the login bug"}\n')

    async def run():
        orch = await _build_orch(tmp_path)
        orch.transcript_path = lambda sid: str(transcript)
        drv = _attach_stub_driver(orch, "s1")
        await orch._persist_status("s1", "done")   # adopts → 1 event
        while not drv._events.empty():
            drv._events.get_nowait()
        await orch._persist_status("s1", "done")   # same title → no event
        return drv._events.empty()

    assert asyncio.run(run()) is True


def test_auto_name_does_not_clobber_manual_rename_during_read(tmp_path, monkeypatch):
    """Re-check pin after async read: a manual rename that lands during the threaded
    read must not be overwritten by Claude's title (TOCTOU fix)."""
    async def run():
        orch = await _build_orch(tmp_path)
        transcript = tmp_path / "claude-s1.jsonl"
        transcript.write_text('{"type":"ai-title","aiTitle":"Claude Title"}\n')
        orch.transcript_path = lambda sid: str(transcript)

        import server.sessions.orchestrator as orch_mod

        def fake_read(path):
            # Simulate a manual rename landing during the (threaded) title read.
            orch._records["s1"].name = "Manual Name"
            orch._records["s1"].name_is_auto = False
            return "Claude Title"

        monkeypatch.setattr(orch_mod, "read_ai_title_from_file", fake_read)

        await orch._persist_status("s1", "done")
        rec = orch.record("s1")
        return rec.name, rec.name_is_auto

    name, is_auto = asyncio.run(run())
    assert name == "Manual Name"
    assert is_auto is False
