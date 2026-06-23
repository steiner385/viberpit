# server/tests/test_account_command.py
import pytest
from server.sessions.commands.catalog import build_catalog
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.types import SessionRecord


def test_account_command_is_in_catalog():
    names = {c.name for c in build_catalog({"commands": []})}
    assert "account" in names
    acct = next(c for c in build_catalog({"commands": []}) if c.name == "account")
    assert acct.kind == "control"


@pytest.mark.asyncio
async def test_run_command_account_calls_manual_switch(monkeypatch):
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    rec = SessionRecord(id="s1", name="n", account="a", claude_session_id="c1",
                        cols=80, rows=24, cwd="/tmp")
    orch._records = {"s1": rec}
    orch._drivers = {"s1": object()}
    seen = {}
    async def _switch(self, sid, acct): seen["acct"] = acct
    monkeypatch.setattr(SessionOrchestrator, "switch_account_manual", _switch)

    result = await orch.run_command("s1", "account", "acct-b")
    assert seen["acct"] == "acct-b"
    assert result["name"] == "account"
    assert result["render"] == "notice"
