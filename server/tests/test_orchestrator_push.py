# server/tests/test_orchestrator_push.py
"""Tests for AUT-02: debounced push on transition into a top-tier needs-you status."""
from __future__ import annotations

import pytest
from server.sessions.types import SessionRecord


def _make_orch():
    from server.sessions.orchestrator import SessionOrchestrator
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    orch._records = {}
    orch._pushed = set()
    return orch


def _add_record(orch, session_id: str, name: str = "test-session", status: str = "idle") -> None:
    orch._records[session_id] = SessionRecord(
        id=session_id,
        name=name,
        account="acct-a",
        claude_session_id="",
        cols=80,
        rows=24,
        cwd="/tmp",
        status=status,
    )


@pytest.mark.asyncio
async def test_push_fires_once_on_entry_into_waiting_input(monkeypatch):
    """Transition idle→waiting_input fires push once; repeat call does NOT fire again."""
    import server.sessions.orchestrator as orch_mod
    calls = []

    async def fake_notify(title, body, *, url=None):
        calls.append((title, body))

    monkeypatch.setattr(orch_mod.push_mod, "notify", fake_notify)

    orch = _make_orch()
    _add_record(orch, "s1", name="my-session", status="idle")

    await orch._maybe_push("s1", "waiting_input")
    assert len(calls) == 1
    assert "my-session" in calls[0][1]
    assert "waiting_input" in calls[0][1]

    # Second call with same push-worthy status — should NOT fire again (debounce)
    await orch._maybe_push("s1", "waiting_input")
    assert len(calls) == 1  # still 1


@pytest.mark.asyncio
async def test_push_re_fires_after_non_push_status_clears_debounce(monkeypatch):
    """waiting_input→in_progress clears debounce; a later waiting_input fires again."""
    import server.sessions.orchestrator as orch_mod
    calls = []

    async def fake_notify(title, body, *, url=None):
        calls.append((title, body))

    monkeypatch.setattr(orch_mod.push_mod, "notify", fake_notify)

    orch = _make_orch()
    _add_record(orch, "s1", name="my-session", status="idle")

    # First entry fires
    await orch._maybe_push("s1", "waiting_input")
    assert len(calls) == 1

    # Non-push-worthy status clears the debounce
    await orch._maybe_push("s1", "in_progress")
    assert len(calls) == 1  # no fire on in_progress

    # Re-entry fires again
    await orch._maybe_push("s1", "waiting_input")
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_push_does_not_fire_on_done(monkeypatch):
    """in_progress→done does NOT fire push (not a push-worthy status)."""
    import server.sessions.orchestrator as orch_mod
    calls = []

    async def fake_notify(title, body, *, url=None):
        calls.append((title, body))

    monkeypatch.setattr(orch_mod.push_mod, "notify", fake_notify)

    orch = _make_orch()
    _add_record(orch, "s1", name="my-session", status="in_progress")

    await orch._maybe_push("s1", "done")
    assert len(calls) == 0


@pytest.mark.asyncio
async def test_push_fires_on_needs_attention(monkeypatch):
    """needs_attention (crashed/failed) is push-worthy and fires."""
    import server.sessions.orchestrator as orch_mod
    calls = []

    async def fake_notify(title, body, *, url=None):
        calls.append((title, body))

    monkeypatch.setattr(orch_mod.push_mod, "notify", fake_notify)

    orch = _make_orch()
    _add_record(orch, "s1", name="crashed-session", status="in_progress")

    await orch._maybe_push("s1", "needs_attention")
    assert len(calls) == 1
    assert "crashed-session" in calls[0][1]
