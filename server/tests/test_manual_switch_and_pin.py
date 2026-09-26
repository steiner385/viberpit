# server/tests/test_manual_switch_and_pin.py
import asyncio
import threading
from unittest import mock

import pytest
from viberpit.account_names import InvalidAccountName
from viberpit.selector import Picked
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.engine import AgentEngine
from server.sessions.model_policy import quota_model
from server.sessions.types import SessionRecord
import server.sessions.orchestrator as orch_mod


def _orch_with(rec):
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    orch._records = {rec.id: rec}
    orch._drivers = {rec.id: object()}
    return orch


def _mock_known_accounts(monkeypatch, names, *, before_read=lambda: None, eligible=None):
    """Make ccswitch_accounts.list_accounts() return the given account names, and
    eligible_enabled_accounts() report every one of them as engine-eligible unless
    `eligible` overrides that (see test_switch_account_manual_rejects_wrong_engine)."""
    def list_accounts():
        before_read()
        return [{"name": n} for n in names]

    monkeypatch.setattr(orch_mod.ccswitch_accounts, "list_accounts", list_accounts)
    monkeypatch.setattr(orch_mod, "_eligible_enabled_accounts",
                        lambda **_kw: list(names if eligible is None else eligible))


@pytest.mark.asyncio
async def test_switch_account_manual_pins_and_reassigns(monkeypatch):
    rec = SessionRecord(id="s1", name="n", account="a", engine=AgentEngine.CLAUDE_CODE, engine_session_id="c1",
                        cols=80, rows=24, cwd="/tmp")
    orch = _orch_with(rec)
    loop_thread = threading.get_ident()
    enumeration_threads = []

    _mock_known_accounts(monkeypatch, ["a", "b"],
                         before_read=lambda: enumeration_threads.append(threading.get_ident()))
    seen = {}
    async def _reassign(self, r, new, *, reason):
        seen["new"] = new; seen["reason"] = reason; r.account = new
        return "rotated"
    async def _persist_pin(self, sid, pinned): seen["pinned"] = pinned
    monkeypatch.setattr(SessionOrchestrator, "_reassign", _reassign)
    monkeypatch.setattr(SessionOrchestrator, "_persist_pin", _persist_pin)

    result = await orch.switch_account_manual("s1", "b")
    assert len(enumeration_threads) == 1
    assert enumeration_threads[0] != loop_thread
    assert result == "rotated"
    assert rec.account_pinned is True
    assert seen == {"new": "b", "reason": "manual", "pinned": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("replace_record", [False, True])
async def test_switch_account_manual_rechecks_session_after_enumeration(monkeypatch, replace_record):
    rec = SessionRecord(id="s1", name="n", account="a", engine=AgentEngine.CLAUDE_CODE,
                        engine_session_id="c1", cols=80, rows=24, cwd="/tmp")
    orch = _orch_with(rec)
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    finish = threading.Event()

    def wait_for_session_change():
        loop.call_soon_threadsafe(started.set)
        assert finish.wait(timeout=5), "test did not release account enumeration"

    _mock_known_accounts(monkeypatch, ["a", "b"], before_read=wait_for_session_change)
    orch._reassign = mock.AsyncMock(return_value="rotated")
    orch._persist_pin = mock.AsyncMock()
    task = asyncio.create_task(orch.switch_account_manual("s1", "b"))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        del orch._records["s1"]
        if replace_record:
            replacement = SessionRecord(id="s1", name="replacement", account="a",
                                        engine=AgentEngine.CLAUDE_CODE, engine_session_id="c2",
                                        cols=80, rows=24, cwd="/tmp")
            orch._records["s1"] = replacement
    finally:
        finish.set()
    if replace_record:
        assert await task == "rotated"
        orch._reassign.assert_awaited_once_with(replacement, "b", reason="manual")
        assert replacement.account_pinned
    else:
        with pytest.raises(KeyError, match="s1"):
            await task
        orch._reassign.assert_not_awaited()
        orch._persist_pin.assert_not_awaited()
    assert not rec.account_pinned


@pytest.mark.asyncio
async def test_switch_account_manual_does_not_pin_on_refusal(monkeypatch):
    """The pin must follow success, not the attempt (#1275): an engine that can't
    rotate is refused by _reassign WITHOUT moving the session, so pinning
    unconditionally would record "pinned to new_account" against a session still
    running on its ORIGINAL account."""
    rec = SessionRecord(id="s1", name="n", account="a", engine=AgentEngine.CODEX, engine_session_id="c1",
                        cols=80, rows=24, cwd="/tmp")
    orch = _orch_with(rec)
    _mock_known_accounts(monkeypatch, ["a", "b"])
    seen = {"pinned_calls": 0}
    async def _reassign(self, r, new, *, reason):
        return "unsupported"
    async def _persist_pin(self, sid, pinned): seen["pinned_calls"] += 1
    monkeypatch.setattr(SessionOrchestrator, "_reassign", _reassign)
    monkeypatch.setattr(SessionOrchestrator, "_persist_pin", _persist_pin)

    result = await orch.switch_account_manual("s1", "b")
    assert result == "unsupported"
    assert rec.account_pinned is False
    assert seen["pinned_calls"] == 0


@pytest.mark.asyncio
async def test_switch_account_manual_pins_on_a_new_thread_switch(monkeypatch):
    """A lossy-but-real switch (account_switch_new_thread) still counts as success
    for pinning purposes — the session DID move, just without thread continuity."""
    rec = SessionRecord(id="s1", name="n", account="a", engine=AgentEngine.CODEX, engine_session_id="c1",
                        cols=80, rows=24, cwd="/tmp")
    orch = _orch_with(rec)
    _mock_known_accounts(monkeypatch, ["a", "b"])
    async def _reassign(self, r, new, *, reason):
        r.account = new
        return "rotated_new_thread"
    async def _persist_pin(self, sid, pinned): rec.account_pinned = pinned
    monkeypatch.setattr(SessionOrchestrator, "_reassign", _reassign)
    monkeypatch.setattr(SessionOrchestrator, "_persist_pin", _persist_pin)

    result = await orch.switch_account_manual("s1", "b")
    assert result == "rotated_new_thread"
    assert rec.account_pinned is True


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
async def test_switch_account_manual_rejects_wrong_engine(monkeypatch, caplog):
    """#2002: a manual /account switch onto an account that can't run this
    session's engine (e.g. a Claude-only account on a Codex session) must be
    refused BEFORE _reassign ever touches the live driver — not discovered only
    after the credential authority rejects the login and the old driver is
    already gone."""
    rec = SessionRecord(id="s1", name="n", account="a", engine=AgentEngine.CODEX, engine_session_id="c1",
                        cols=80, rows=24, cwd="/tmp")
    orch = _orch_with(rec)
    # "b" is a known account (passes the ledger-membership check) but not
    # eligible for Codex (e.g. it's a Claude-only account) — the real-world
    # shape of #2002.
    _mock_known_accounts(monkeypatch, ["a", "b"], eligible=["a"])
    called = {"reassign": False}
    async def _reassign(self, r, new, *, reason): called["reassign"] = True
    monkeypatch.setattr(SessionOrchestrator, "_reassign", _reassign)

    with caplog.at_level("WARNING"):
        result = await orch.switch_account_manual("s1", "b")

    assert result == "invalid_target"
    assert called["reassign"] is False, "the old driver must never be touched"
    assert rec.account_pinned is False
    assert rec.account == "a", "the session must stay on its original account"
    assert not caplog.records, "a config mismatch must not log at WARNING+ (auto-files as an error)"


@pytest.mark.asyncio
async def test_switch_account_manual_rejects_codex_account_on_claude_session(monkeypatch, caplog):
    """#1998: the mirror of #2002 — a Claude Code session's /account onto an
    OpenAI/Codex account has no Claude credential slot, so composing its config
    dir raised FileNotFoundError out of _reassign_locked (an ERROR auto-error).
    It must be a clean, INFO-level refusal before _reassign runs."""
    rec = SessionRecord(id="s1", name="n", account="a", engine=AgentEngine.CLAUDE_CODE, engine_session_id="c1",
                        cols=80, rows=24, cwd="/tmp")
    orch = _orch_with(rec)
    _mock_known_accounts(monkeypatch, ["a", "chatgpt-x"], eligible=["a"])
    seen = {}

    def eligible(**kw):
        seen.update(kw)
        return ["a"]

    monkeypatch.setattr(orch_mod, "_eligible_enabled_accounts", eligible)
    called = {"reassign": False}
    async def _reassign(self, r, new, *, reason): called["reassign"] = True
    monkeypatch.setattr(SessionOrchestrator, "_reassign", _reassign)

    with caplog.at_level("WARNING"):
        result = await orch.switch_account_manual("s1", "chatgpt-x")

    assert result == "invalid_target"
    assert seen["require_engine"] == AgentEngine.CLAUDE_CODE.value
    assert called["reassign"] is False
    assert rec.account == "a" and rec.account_pinned is False
    assert not caplog.records


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
                        cols=80, rows=24, cwd="/tmp", account_pinned=True, model="sonnet")
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
    # select_account gained a load-aware `load=` kwarg (master #137) and a
    # per-model quota key `model=`; accept and record both.
    seen = {}

    def _select(exclude=frozenset(), load=None, soft_exclude=frozenset(),
                engine=AgentEngine.CLAUDE_CODE, model=None, **_kw):
        seen["model"] = model
        return Picked("b")

    monkeypatch.setattr("server.sessions.orchestrator.select_account", _select)

    await orch._rotate_away(rec, reason="auth_401")
    assert rec.account_pinned is False  # hard-failure recovery overrides the pin
    # The replacement account is picked for the model this session runs, not for
    # "every window" — a Fable-exhausted account still serves Sonnet.
    assert seen["model"] == quota_model(rec.model, engine=rec.engine) == "sonnet"
