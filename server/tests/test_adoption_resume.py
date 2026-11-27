"""The adopting host's pickup (spec 002 FR-204/FR-205, #949 Task 19):
``adoption.pickup_adopted`` / ``take_adopted`` on a real orchestrator with the driver
build and the prompt send stubbed — the two things that would touch a CLI.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select as sa_select

from server.db import db_session
from server.db.models import Host, Session as DbSession, SessionQueue
from server.sessions import adoption, holds, provenance
from server.sessions.orchestrator import RESUME_HOLD_NUDGE, SessionOrchestrator
from server.sessions.types import SYSTEM_IDENTITY
from server.tests.fakes.fake_factory import fake_sdk_factory

ME = "w2"


async def _adopted(sid: str, *, from_status: str, status: str | None = None,
                   host: str = ME, **kw) -> None:
    """A row as the primary's pass leaves it: restamped to *host*, normalized, marked,
    with its ``interrupted{host_lost}`` event."""
    fields = dict(id=sid, name=f"goal {sid}", account_name="acct", engine="claude_code",
                  engine_session_id=f"c-{sid}", cwd=f"/tmp/{sid}", repo="o/r", branch=f"b-{sid}",
                  host=host, adopted_from="w1", moved_from="w1",
                  status=status or ("idle" if from_status in ("in_progress", "working",
                                                              "waiting_question") else from_status),
                  initial_prompt=f"please do {sid}")
    fields.update(kw)
    async with db_session() as db:
        db.add(DbSession(**fields))
    await provenance.record(sid, provenance.INTERRUPTED, {
        "kind": "host_lost", "fromStatus": from_status, "host": "w1"})


async def _row(sid: str) -> DbSession:
    async with db_session() as db:
        return (await db.execute(sa_select(DbSession).where(DbSession.id == sid))).scalar_one()


async def _moved(sid: str) -> list[dict]:
    return [e["detail"] for e in await provenance.list_events(sid)
            if e["kind"] == provenance.MOVED_HOST]


class _Driver: ...


def _orch(monkeypatch, *, transcript: bool, role: str = "worker",
          driver: bool = True) -> tuple[SessionOrchestrator, dict]:
    monkeypatch.setenv("VIBERPIT_HOST_NAME", ME)
    monkeypatch.setenv("VIBERPIT_DAEMON_ROLE", role)
    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0)
    calls: dict = {"ensure": [], "prompt": [], "find": []}

    async def ensure_driver(sid, identity=SYSTEM_IDENTITY):
        calls["ensure"].append((sid, identity, orch._records[sid].engine_session_id))
        if not driver:
            return None
        orch._drivers[sid] = _Driver()
        return orch._drivers[sid]

    async def send_prompt(sid, text, attachment_ids=None, *, replayable=None, **_kw):
        calls["prompt"].append((sid, text, replayable))
        return "sent"

    def find(config_dir, csid, cwd=None):
        calls["find"].append((config_dir, csid, cwd))
        return f"{config_dir}/projects/x/{csid}.jsonl" if transcript else None

    orch.ensure_driver = ensure_driver            # type: ignore[assignment,method-assign]
    orch.send_prompt = send_prompt                # type: ignore[assignment,method-assign]
    orch._find_transcript = find                  # type: ignore[assignment,method-assign]
    return orch, calls


@pytest.mark.asyncio
async def test_mid_turn_session_with_transcript_resumes_and_is_nudged(isolated_db, monkeypatch):
    orch, calls = _orch(monkeypatch, transcript=True)
    try:
        await _adopted("a", from_status="in_progress")
        assert await adoption.pickup_adopted(orch) == 1
        assert calls["ensure"] == [("a", SYSTEM_IDENTITY, "c-a")]     # resume by its id
        assert calls["find"][0][1:] == ("c-a", "/tmp/a")
        (sid, text, replayable), = calls["prompt"]
        assert sid == "a" and replayable is False
        assert text.startswith("This session was moved from w1 to w2 after w1 became unreachable")
        assert "Continue where you left off" in text
        rec = orch._records["a"]
        assert rec.host == ME and rec.engine_session_id == "c-a"
        assert await _moved("a") == [{"from": "w1", "to": ME, "reason": "host_lost",
                                      "resume": "transcript", "fromStatus": "in_progress"}]
        assert (await _row("a")).adopted_from is None                   # marker cleared
        assert await adoption.pickup_adopted(orch) == 0                  # idempotent
    finally:
        await orch.stop()


@pytest.mark.asyncio
async def test_no_transcript_means_a_fresh_conversation_with_the_continuity_prompt(
        isolated_db, monkeypatch):
    orch, calls = _orch(monkeypatch, transcript=False)
    try:
        await _adopted("a", from_status="working", initial_prompt="x" * 5000)
        await adoption.pickup_adopted(orch)
        assert calls["ensure"] == [("a", SYSTEM_IDENTITY, "")]          # a NEW conversation
        text = calls["prompt"][0][1]
        for needle in ("moved from w1 to w2", "could not be recovered", "Repo: o/r",
                       "branch: b-a", "unpushed work may still exist on w1",
                       "The goal was: goal a", "Re-establish context and continue"):
            assert needle in text, needle
        assert "x" * 2000 + " …" in text and "x" * 2001 not in text   # bounded quote
        assert (await _moved("a"))[0]["resume"] == "fresh"
    finally:
        await orch.stop()


@pytest.mark.asyncio
async def test_codex_session_cannot_resume_across_hosts(isolated_db, monkeypatch):
    """A Codex thread lives in the dead host's CODEX_HOME: fresh, never a lookup."""
    orch, calls = _orch(monkeypatch, transcript=True)
    try:
        await _adopted("a", from_status="in_progress", engine="codex", engine_session_id="t-1")
        await adoption.pickup_adopted(orch)
        assert calls["find"] == [] and calls["ensure"] == [("a", SYSTEM_IDENTITY, "")]
        assert (await _moved("a"))[0]["resume"] == "fresh"
    finally:
        await orch.stop()


@pytest.mark.asyncio
async def test_lost_hold_rides_the_nudge_and_is_recorded(isolated_db, monkeypatch):
    """FR-305 on an adopted row: the ask the dead host left open becomes ``hold_lost`` +
    the record's ``lost_hold``, and the nudge hands the agent its question back."""
    orch, calls = _orch(monkeypatch, transcript=True)
    try:
        await _adopted("a", from_status="waiting_question")
        assert await holds.open_hold("a", "question", {"question": "Which colour?"})
        await adoption.pickup_adopted(orch)
        rec = orch._records["a"]
        assert rec.lost_hold and rec.lost_hold["kind"] == "question"
        kinds = [e["kind"] for e in await provenance.list_events("a")]
        assert provenance.HOLD_LOST in kinds
        text = calls["prompt"][0][1]
        assert RESUME_HOLD_NUDGE.split("{")[0].strip() in text and "Which colour?" in text
        assert (await _moved("a"))[0]["resume"] == "transcript"
    finally:
        await orch.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("from_status,extra", [
    ("done", {}),                                   # resting: nothing was cut off
    ("waiting_question", {}),                       # waiting, but no hold content to re-ask
    ("in_progress", {"auto_resume_opt_out": True}),  # the per-session veto (FR-306)
])
async def test_resting_or_vetoed_sessions_are_deferred_not_started(
        isolated_db, monkeypatch, from_status, extra):
    orch, calls = _orch(monkeypatch, transcript=True)
    try:
        await _adopted("a", from_status=from_status, **extra)
        await adoption.pickup_adopted(orch)
        assert calls["ensure"] == [] and calls["prompt"] == []
        assert (await _moved("a"))[0]["resume"] == "deferred"
        assert (await _row("a")).adopted_from is None
        assert "a" in orch._records and orch._records["a"].host == ME
        # The next prompt the user sends carries the notice.
        assert any("moved from w1 to w2" in t for t in orch._pending_prompt_preambles["a"])
    finally:
        await orch.stop()


@pytest.mark.asyncio
async def test_deferred_without_transcript_forgets_the_dead_conversation_id(isolated_db, monkeypatch):
    orch, _ = _orch(monkeypatch, transcript=False)
    try:
        await _adopted("a", from_status="done")
        await adoption.pickup_adopted(orch)
        assert orch._records["a"].engine_session_id == ""       # next prompt starts fresh
        assert any("could not be recovered" in t for t in orch._pending_prompt_preambles["a"])
    finally:
        await orch.stop()


@pytest.mark.asyncio
async def test_failed_resume_is_recorded_and_clears_the_marker(isolated_db, monkeypatch):
    orch, calls = _orch(monkeypatch, transcript=True, driver=False)
    try:
        await _adopted("a", from_status="in_progress")
        await adoption.pickup_adopted(orch)
        assert calls["prompt"] == []
        assert (await _moved("a"))[0]["resume"] == "failed"
        assert (await _row("a")).adopted_from is None
        # Left on its interrupted banner, resumable by a person.
        assert orch._records["a"].interrupted_by_restart is True
    finally:
        await orch.stop()


@pytest.mark.asyncio
async def test_pickup_takes_only_rows_stamped_here_and_not_mid_move(isolated_db, monkeypatch):
    orch, calls = _orch(monkeypatch, transcript=True)
    try:
        await _adopted("mine", from_status="in_progress")
        await _adopted("theirs", from_status="in_progress", host="w3")
        await _adopted("moving", from_status="in_progress", moving_to="w3")
        await _adopted("queued", from_status="in_progress", status="queued")  # the drain's
        await _adopted("ended", from_status="in_progress", status="ended")
        assert await adoption.pickup_adopted(orch) == 1
        assert [c[0] for c in calls["ensure"]] == ["mine"]
        for sid in ("theirs", "moving", "queued", "ended"):
            assert (await _row(sid)).adopted_from == "w1", sid
    finally:
        await orch.stop()


@pytest.mark.asyncio
async def test_primary_takes_rows_stamped_with_its_own_name(isolated_db, monkeypatch):
    orch, calls = _orch(monkeypatch, transcript=True, role="primary")
    try:
        await _adopted("a", from_status="in_progress", host=ME)
        await _adopted("b", from_status="in_progress", host="rocky")   # another live host's
        assert await adoption.pickup_adopted(orch) == 1
        assert [c[0] for c in calls["ensure"]] == ["a"]
    finally:
        await orch.stop()


@pytest.mark.asyncio
async def test_fenced_or_draining_daemon_takes_nothing(isolated_db, monkeypatch):
    orch, calls = _orch(monkeypatch, transcript=True)
    try:
        await _adopted("a", from_status="in_progress")
        orch._hb_fenced = True
        assert await adoption.pickup_adopted(orch) == 0
        orch._hb_fenced = False
        orch._draining = True
        assert await adoption.pickup_adopted(orch) == 0
        assert calls["ensure"] == []
    finally:
        await orch.stop()


# ── capacity (FR-205) ─────────────────────────────────────────────────────────────────

async def _cap_this_host(cap: int) -> None:
    async with db_session() as db:
        db.add(Host(name=ME, endpoint="https://w2:8443", enabled=True, healthy=True,
                    max_sessions=cap))


async def _live(sid: str) -> None:
    async with db_session() as db:
        db.add(DbSession(id=sid, name=sid, account_name="acct", engine_session_id=f"c-{sid}",
                         status="in_progress", cwd=f"/tmp/{sid}", host=ME))


@pytest.mark.asyncio
async def test_at_capacity_the_adopted_session_lands_queued_then_the_drain_resumes_it(
        isolated_db, monkeypatch):
    orch, calls = _orch(monkeypatch, transcript=True)
    try:
        await _cap_this_host(1)
        await _live("busy")                       # this host's one slot is taken
        await orch.rehydrate()
        await _adopted("a", from_status="in_progress")

        assert await adoption.pickup_adopted(orch) == 1
        row = await _row("a")
        assert row.status == "queued" and row.adopted_from == "w1"      # marker kept
        assert orch._records["a"].status == "queued"
        assert calls["ensure"] == []
        async with db_session() as db:
            entry = (await db.execute(sa_select(SessionQueue).where(
                SessionQueue.session_id == "a"))).scalar_one()
        assert entry.params[adoption.ADOPTED_RESUME_PARAM] is True
        assert entry.params["queue_reason"] == adoption.QUEUE_REASON
        assert [m["resume"] for m in await _moved("a")] == ["queued"]
        assert await adoption.pickup_adopted(orch) == 0               # the drain's now

        # A slot frees: the drain takes it back through take_adopted, not a spawn.
        orch._records["busy"].status = "ended"
        assert await orch._maybe_start_queued() == 1
        assert calls["ensure"] == [("a", SYSTEM_IDENTITY, "c-a")]
        assert len(calls["prompt"]) == 1
        row = await _row("a")
        assert row.status == "idle" and row.adopted_from is None
        assert [m["resume"] for m in await _moved("a")] == ["transcript", "queued"]  # newest first
        async with db_session() as db:
            assert (await db.execute(sa_select(SessionQueue).where(
                SessionQueue.session_id == "a"))).scalar_one_or_none() is None
    finally:
        await orch.stop()


@pytest.mark.asyncio
async def test_drain_leaves_another_hosts_adopted_entry_alone(isolated_db, monkeypatch):
    orch, calls = _orch(monkeypatch, transcript=True)
    try:
        await _adopted("a", from_status="in_progress", host="w3", status="queued")
        from server.sessions import session_queue as sq
        await sq.enqueue("a", {"queue_reason": adoption.QUEUE_REASON,
                               adoption.ADOPTED_RESUME_PARAM: True})
        assert await orch._maybe_start_queued() == 0
        assert calls["ensure"] == [] and (await _row("a")).status == "queued"
        assert await sq.entry_for("a") is not None
    finally:
        await orch.stop()
