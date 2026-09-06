# server/tests/test_account_command.py
import pytest
from server.codex.capabilities import CODEX_CAPABILITIES
from server.sessions.claude_driver import ClaudeAgentDriver
from server.sessions.commands.catalog import build_catalog
from server.sessions.engine import AgentCapabilities
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.engine import AgentEngine
from server.sessions.types import SessionRecord


@pytest.mark.parametrize("caps", [ClaudeAgentDriver.capabilities, CODEX_CAPABILITIES])
def test_account_command_is_in_catalog(caps):
    """Offered on every engine that can actually move a session's account —
    losslessly (Claude) or as a fresh thread (Codex)."""
    names = {c.name for c in build_catalog({"commands": []}, caps)}
    assert "account" in names
    acct = next(c for c in build_catalog({"commands": []}, caps) if c.name == "account")
    assert acct.kind == "control"
    assert acct.source == "app"


def test_account_command_is_withheld_from_an_engine_that_cannot_switch():
    """An engine with neither account capability must not advertise `/account`:
    the command can only park the session on its original account there, and the
    palette offering it is how a user learns that the hard way."""
    stuck = AgentCapabilities(account_rotation=False, account_switch_new_thread=False)
    assert "account" not in {c.name for c in build_catalog({"commands": []}, stuck)}


@pytest.mark.asyncio
async def test_run_command_account_calls_manual_switch(monkeypatch):
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    rec = SessionRecord(id="s1", name="n", account="a", engine=AgentEngine.CLAUDE_CODE, engine_session_id="c1",
                        cols=80, rows=24, cwd="/tmp")
    orch._records = {"s1": rec}
    orch._drivers = {"s1": object()}
    seen = {}
    async def _switch(self, sid, acct):
        seen["acct"] = acct
        return "rotated"
    monkeypatch.setattr(SessionOrchestrator, "switch_account_manual", _switch)

    result = await orch.run_command("s1", "account", "acct-b")
    assert seen["acct"] == "acct-b"
    assert result["name"] == "account"
    assert result["render"] == "notice"
    assert result["data"]["text"] == "Account → acct-b"


@pytest.mark.asyncio
async def test_run_command_account_reports_a_new_thread_switch_honestly(monkeypatch):
    """An engine that can only start a fresh thread (account_switch_new_thread,
    e.g. Codex) must say so — not the same "Account → x" text a thread-preserving
    switch gets, which would read as "your conversation continued"."""
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    rec = SessionRecord(id="s1", name="n", account="a", engine=AgentEngine.CODEX, engine_session_id="c1",
                        cols=80, rows=24, cwd="/tmp")
    orch._records = {"s1": rec}
    orch._drivers = {"s1": object()}
    async def _switch(self, sid, acct):
        return "rotated_new_thread"
    monkeypatch.setattr(SessionOrchestrator, "switch_account_manual", _switch)

    result = await orch.run_command("s1", "account", "acct-b")
    text = result["data"]["text"]
    assert "acct-b" in text
    assert "fresh" in text.lower() or "new one" in text.lower()


@pytest.mark.asyncio
async def test_run_command_account_refuses_honestly_when_the_engine_cannot_switch(monkeypatch):
    """#1275: the old handler reported "Account → acct" unconditionally, even when
    switch_account_manual had silently refused and left the session on its
    ORIGINAL account. The command must say the switch did not happen."""
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    rec = SessionRecord(id="s1", name="n", account="a", engine=AgentEngine.CODEX, engine_session_id="c1",
                        cols=80, rows=24, cwd="/tmp")
    orch._records = {"s1": rec}
    orch._drivers = {"s1": object()}
    async def _switch(self, sid, acct):
        return "unsupported"
    monkeypatch.setattr(SessionOrchestrator, "switch_account_manual", _switch)

    result = await orch.run_command("s1", "account", "acct-b")
    text = result["data"]["text"]
    assert not text.startswith("Account →"), "must not claim the switch happened"
    assert "Can't switch" in text
