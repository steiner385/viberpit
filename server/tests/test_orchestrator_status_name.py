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
    """Give a (detached) session a live-driver stand-in exposing just an _out queue."""
    drv = types.SimpleNamespace(_out=asyncio.Queue())
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
    assert event == {"type": "status", "sessionId": "s1", "status": "waiting_input"}


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
    assert event == {"type": "status", "sessionId": "s1", "status": "waiting_external"}


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
        while not drv._out.empty():
            events.append(drv._out.get_nowait())
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
        while not drv._out.empty():
            drv._out.get_nowait()
        await orch._persist_status("s1", "done")   # same title → no event
        return drv._out.empty()

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
