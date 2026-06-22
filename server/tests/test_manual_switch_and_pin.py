# server/tests/test_manual_switch_and_pin.py
import pytest
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.types import SessionRecord


def _orch_with(rec):
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    orch._records = {rec.id: rec}
    orch._drivers = {rec.id: object()}
    return orch


@pytest.mark.asyncio
async def test_switch_account_manual_pins_and_reassigns(monkeypatch):
    rec = SessionRecord(id="s1", name="n", account="a", claude_session_id="c1",
                        cols=80, rows=24, cwd="/tmp")
    orch = _orch_with(rec)
    seen = {}
    async def _reassign(self, r, new, *, reason):
        seen["new"] = new; seen["reason"] = reason; r.account = new
    async def _persist_pin(self, sid, pinned): seen["pinned"] = pinned
    monkeypatch.setattr(SessionOrchestrator, "_reassign", _reassign)
    monkeypatch.setattr(SessionOrchestrator, "_persist_pin", _persist_pin)

    await orch.switch_account_manual("s1", "b")
    assert rec.account_pinned is True
    assert seen == {"new": "b", "reason": "manual", "pinned": True}


@pytest.mark.asyncio
async def test_rotate_away_clears_pin(monkeypatch):
    rec = SessionRecord(id="s1", name="n", account="a", claude_session_id="c1",
                        cols=80, rows=24, cwd="/tmp", account_pinned=True)
    orch = _orch_with(rec)
    orch._recovery = type("R", (), {
        "quarantined": lambda self, now: frozenset(),
        "record_rotation": lambda self, sid, now: None,
    })()
    orch._clock = lambda: 0.0
    async def _reassign(self, r, new, *, reason): r.account = new
    async def _persist_pin(self, sid, pinned): rec.account_pinned = pinned
    monkeypatch.setattr(SessionOrchestrator, "_reassign", _reassign)
    monkeypatch.setattr(SessionOrchestrator, "_persist_pin", _persist_pin)
    monkeypatch.setattr("server.sessions.orchestrator.select_account", lambda exclude=frozenset(): "b")

    await orch._rotate_away(rec, reason="auth_401")
    assert rec.account_pinned is False  # hard-failure recovery overrides the pin
