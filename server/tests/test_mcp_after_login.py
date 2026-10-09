import pytest

from server.mcp_health import after_login


class _Orch:
    def __init__(self):
        self.calls = []

    async def reconnect_mcp_everywhere(self, server, statuses):
        self.calls.append(("rc", server, tuple(statuses)))
        return 2

    async def probe_mcp(self, account=None):
        self.calls.append(("probe", account))
        return {"rows": [], "skipped": []}


@pytest.mark.asyncio
async def test_after_login_refreshes_then_reconnects_then_probes(monkeypatch):
    refreshed = []
    monkeypatch.setattr(after_login, "_refresh", lambda acct: refreshed.append(acct) or True)
    orch = _Orch()
    out = await after_login.after_login(orch, "notion", ["alice", "bob"])
    assert refreshed == ["alice", "bob"]
    assert orch.calls[0] == ("rc", "notion", ("needs-auth", "failed"))
    assert ("probe", None) in orch.calls
    assert out == {"refreshed": 2, "reconnected": 2}


@pytest.mark.asyncio
async def test_after_login_survives_refresh_and_probe_failures(monkeypatch):
    def boom(acct):
        raise OSError("sshfs gone")
    monkeypatch.setattr(after_login, "_refresh", boom)
    orch = _Orch()

    async def bad_probe(account=None):
        raise RuntimeError("x")
    orch.probe_mcp = bad_probe
    out = await after_login.after_login(orch, "notion", ["alice"])
    assert out == {"refreshed": 0, "reconnected": 2}
