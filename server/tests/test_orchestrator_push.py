# server/tests/test_orchestrator_push.py
"""Tests for AUT-02 (debounced push on transition into a top-tier needs-you status)
and #1354 (debounced `session_ended` notify on transition into a finished status)."""
from __future__ import annotations

import pytest
from server.sessions.engine import AgentEngine
from server.sessions.types import SessionRecord


def _make_orch():
    from server.sessions.orchestrator import SessionOrchestrator
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    orch._records = {}
    orch._pushed = set()
    orch._session_finished_notified = set()
    return orch


def _add_record(orch, session_id: str, name: str = "test-session", status: str = "idle") -> None:
    orch._records[session_id] = SessionRecord(
        id=session_id,
        name=name,
        account="acct-a",
        engine=AgentEngine.CLAUDE_CODE,
        engine_session_id="",
        cols=80,
        rows=24,
        cwd="/tmp",
        status=status,
    )


def _patch_notify_event(monkeypatch):
    """Patch push_mod.notify_event to record (event_class, title, body, kwargs) instead
    of the real DB-backed routing — precise, unlike relying on its DB-failure fallback."""
    import server.sessions.orchestrator as orch_mod
    calls = []

    async def fake_notify_event(event_class, title, body, **kwargs):
        calls.append((event_class, title, body, kwargs))

    monkeypatch.setattr(orch_mod.push_mod, "notify_event", fake_notify_event)
    return calls


def _classes(calls) -> list[str]:
    return [c[0] for c in calls]


# --------------------------------------------------------------------------- #
# needs_input push (AUT-02)                                                   #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_push_fires_once_on_entry_into_waiting_input(monkeypatch):
    """Transition idle→waiting_input fires push once; repeat call does NOT fire again."""
    calls = _patch_notify_event(monkeypatch)
    orch = _make_orch()
    _add_record(orch, "s1", name="my-session", status="idle")

    await orch._maybe_push("s1", "waiting_input")
    needs_input = [c for c in calls if c[0] == "needs_input"]
    assert len(needs_input) == 1
    assert "my-session" in needs_input[0][2]
    assert "waiting_input" in needs_input[0][2]
    assert needs_input[0][3]["url"] == "/session/s1"

    # Second call with same push-worthy status — should NOT fire again (debounce)
    await orch._maybe_push("s1", "waiting_input")
    assert len([c for c in calls if c[0] == "needs_input"]) == 1  # still 1


@pytest.mark.asyncio
async def test_push_re_fires_after_non_push_status_clears_debounce(monkeypatch):
    """waiting_input→in_progress clears debounce; a later waiting_input fires again."""
    calls = _patch_notify_event(monkeypatch)
    orch = _make_orch()
    _add_record(orch, "s1", name="my-session", status="idle")

    # First entry fires
    await orch._maybe_push("s1", "waiting_input")
    assert _classes(calls).count("needs_input") == 1

    # Non-push-worthy status clears the debounce
    await orch._maybe_push("s1", "in_progress")
    assert _classes(calls).count("needs_input") == 1  # no fire on in_progress

    # Re-entry fires again
    await orch._maybe_push("s1", "waiting_input")
    assert _classes(calls).count("needs_input") == 2


@pytest.mark.asyncio
async def test_push_fires_on_needs_attention(monkeypatch):
    """needs_attention (crashed/failed) is push-worthy and fires."""
    calls = _patch_notify_event(monkeypatch)
    orch = _make_orch()
    _add_record(orch, "s1", name="crashed-session", status="in_progress")

    await orch._maybe_push("s1", "needs_attention")
    needs_input = [c for c in calls if c[0] == "needs_input"]
    assert len(needs_input) == 1
    assert "crashed-session" in needs_input[0][2]


# --------------------------------------------------------------------------- #
# session_ended notify (#1354 kind="session" watch)                           #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_session_ended_fires_once_on_entry_into_done(monkeypatch):
    calls = _patch_notify_event(monkeypatch)
    orch = _make_orch()
    _add_record(orch, "s1", name="my-session", status="in_progress")

    await orch._maybe_push("s1", "done")
    finished = [c for c in calls if c[0] == "session_ended"]
    assert len(finished) == 1
    assert "my-session" in finished[0][2]
    assert finished[0][3]["session_id"] == "s1"
    assert finished[0][3]["url"] == "/session/s1"
    # "done" is not push-worthy — the needs_input push must not also fire.
    assert not any(c[0] == "needs_input" for c in calls)

    # Repeat call with the same finished status does not re-fire (debounce).
    await orch._maybe_push("s1", "done")
    assert len([c for c in calls if c[0] == "session_ended"]) == 1


@pytest.mark.asyncio
async def test_session_ended_fires_on_complete_too(monkeypatch):
    calls = _patch_notify_event(monkeypatch)
    orch = _make_orch()
    _add_record(orch, "s1", name="my-session", status="in_progress")

    await orch._maybe_push("s1", "complete")
    assert _classes(calls).count("session_ended") == 1


@pytest.mark.asyncio
async def test_session_ended_re_fires_after_a_new_turn_completes(monkeypatch):
    calls = _patch_notify_event(monkeypatch)
    orch = _make_orch()
    _add_record(orch, "s1", name="my-session", status="in_progress")

    await orch._maybe_push("s1", "done")
    assert _classes(calls).count("session_ended") == 1

    # A new turn starting clears the debounce.
    await orch._maybe_push("s1", "in_progress")
    assert _classes(calls).count("session_ended") == 1

    # That turn finishing re-fires.
    await orch._maybe_push("s1", "done")
    assert _classes(calls).count("session_ended") == 2


@pytest.mark.asyncio
async def test_session_ended_does_not_fire_on_waiting_input(monkeypatch):
    """Parked-on-user statuses are not "finished" — only needs_input should fire."""
    calls = _patch_notify_event(monkeypatch)
    orch = _make_orch()
    _add_record(orch, "s1", name="my-session", status="in_progress")

    await orch._maybe_push("s1", "waiting_input")
    assert _classes(calls).count("session_ended") == 0
    assert _classes(calls).count("needs_input") == 1
