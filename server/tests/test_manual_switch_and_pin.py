# server/tests/test_manual_switch_and_pin.py
import pytest
from ccswitch.account_names import InvalidAccountName
from ccswitch.selector import Picked
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.engine import AgentEngine
from server.sessions.types import SessionRecord
import server.sessions.orchestrator as orch_mod


def _orch_with(rec):
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    orch._records = {rec.id: rec}
    orch._drivers = {rec.id: object()}
    return orch


def _mock_known_accounts(monkeypatch, names):
    """Make ccswitch_accounts.list_accounts() return the given account names."""
    monkeypatch.setattr(orch_mod.ccswitch_accounts, "list_accounts",
                        lambda: [{"name": n} for n in names])


@pytest.mark.asyncio
async def test_switch_account_manual_pins_and_reassigns(monkeypatch):
    rec = SessionRecord(id="s1", name="n", account="a", engine=AgentEngine.CLAUDE_CODE, engine_session_id="c1",
                        cols=80, rows=24, cwd="/tmp")
    orch = _orch_with(rec)
    _mock_known_accounts(monkeypatch, ["a", "b"])
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
async def test_switch_account_manual_rejects_path_traversal(monkeypatch):
    rec = SessionRecord(id="s1", name="n", account="a", engine=AgentEngine.CLAUDE_CODE, engine_session_id="c1",
                        cols=80, rows=24, cwd="/tmp")
    orch = _orch_with(rec)
    _mock_known_accounts(monkeypatch, ["a", "b"])
    called = {"reassign": False}
    async def _reassign(self, r, new, *, reason): called["reassign"] = True
    async def _persist_pin(self, sid, pinned): pass
    monkeypatch.setattr(SessionOrchestrator, "_reassign", _reassign)
    monkeypatch.setattr(SessionOrchestrator, "_persist_pin", _persist_pin)

    with pytest.raises(InvalidAccountName):
        await orch.switch_account_manual("s1", "../evil")
    assert called["reassign"] is False
    assert rec.account_pinned is False  # never pinned on a rejected switch


@pytest.mark.asyncio
async def test_switch_account_manual_rejects_unknown_account(monkeypatch):
    rec = SessionRecord(id="s1", name="n", account="a", engine=AgentEngine.CLAUDE_CODE, engine_session_id="c1",
                        cols=80, rows=24, cwd="/tmp")
    orch = _orch_with(rec)
    _mock_known_accounts(monkeypatch, ["a", "b"])  # "ghost" is valid-format but unknown
    called = {"reassign": False}
    async def _reassign(self, r, new, *, reason): called["reassign"] = True
    async def _persist_pin(self, sid, pinned): pass
    monkeypatch.setattr(SessionOrchestrator, "_reassign", _reassign)
    monkeypatch.setattr(SessionOrchestrator, "_persist_pin", _persist_pin)

    with pytest.raises(KeyError):
        await orch.switch_account_manual("s1", "ghost")
    assert called["reassign"] is False
    assert rec.account_pinned is False


@pytest.mark.asyncio
async def test_rotate_away_clears_pin(monkeypatch):
    rec = SessionRecord(id="s1", name="n", account="a", engine=AgentEngine.CLAUDE_CODE, engine_session_id="c1",
                        cols=80, rows=24, cwd="/tmp", account_pinned=True)
    orch = _orch_with(rec)
    orch._recovery = type("R", (), {
        "quarantined": lambda self, now: frozenset(),
        "record_rotation": lambda self, sid, now: None,
    })()
    orch._clock = lambda: 0.0
    orch.live_session_load = lambda: {}
    async def _reassign(self, r, new, *, reason): r.account = new
    async def _persist_pin(self, sid, pinned): rec.account_pinned = pinned
    monkeypatch.setattr(SessionOrchestrator, "_reassign", _reassign)
    monkeypatch.setattr(SessionOrchestrator, "_persist_pin", _persist_pin)
    # select_account gained a load-aware `load=` kwarg (master #137); accept it.
    monkeypatch.setattr("server.sessions.orchestrator.select_account",
                        lambda exclude=frozenset(), load=None,
                        engine=AgentEngine.CLAUDE_CODE: Picked("b"))

    await orch._rotate_away(rec, reason="auth_401")
    assert rec.account_pinned is False  # hard-failure recovery overrides the pin
