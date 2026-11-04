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
    monkeypatch.setattr(after_login, "_refresh", lambda acct, server=None: refreshed.append(acct) or True)
    orch = _Orch()
    out = await after_login.after_login(orch, "notion", ["alice", "bob"])
    assert refreshed == ["alice", "bob"]
    assert orch.calls[0] == ("rc", "notion", ("needs-auth", "failed"))
    assert ("probe", None) in orch.calls
    assert out == {"refreshed": 2, "reconnected": 2}


@pytest.mark.asyncio
async def test_after_login_survives_refresh_and_probe_failures(monkeypatch):
    def boom(acct, server=None):
        raise OSError("sshfs gone")
    monkeypatch.setattr(after_login, "_refresh", boom)
    orch = _Orch()

    async def bad_probe(account=None):
        raise RuntimeError("x")
    orch.probe_mcp = bad_probe
    out = await after_login.after_login(orch, "notion", ["alice"])
    assert out == {"refreshed": 0, "reconnected": 2}


@pytest.mark.asyncio
async def test_probe_runs_even_if_reconnect_raises(monkeypatch):
    monkeypatch.setattr(after_login, "_refresh", lambda acct, server=None: True)
    orch = _Orch()

    async def bad_rc(server, statuses):
        raise RuntimeError("fleet down")
    orch.reconnect_mcp_everywhere = bad_rc
    out = await after_login.after_login(orch, "notion", ["alice"])
    assert ("probe", None) in orch.calls
    assert out == {"refreshed": 1, "reconnected": 0}


@pytest.mark.asyncio
async def test_refresh_receives_server(monkeypatch):
    seen = []
    monkeypatch.setattr(after_login.creds, "sync_cfg_mcp_oauth",
                        lambda cfg, server=None: seen.append(server) or False)
    await after_login.after_login(_Orch(), "notion", ["alice"])
    assert seen == ["notion"]


@pytest.fixture
def cleared(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(after_login, "clear_needs_auth_cache", lambda server: seen.append(server) or [])
    return seen


@pytest.mark.asyncio
async def test_after_login_clears_needs_auth_cache(monkeypatch, cleared):
    monkeypatch.setattr(after_login, "_refresh", lambda acct, server=None: True)
    orch = _Orch()
    real_rc = orch.reconnect_mcp_everywhere

    async def rc(server, statuses):
        orch.calls.append(("cleared-before-rc", list(cleared)))
        return await real_rc(server, statuses)
    orch.reconnect_mcp_everywhere = rc
    await after_login.after_login(orch, "notion", ["alice"])
    assert cleared == ["notion"]
    assert orch.calls[0] == ("cleared-before-rc", ["notion"])


@pytest.mark.asyncio
async def test_after_login_probe_false_skips_probe(monkeypatch, cleared):
    monkeypatch.setattr(after_login, "_refresh", lambda acct, server=None: True)
    orch = _Orch()
    await after_login.after_login(orch, "notion", ["alice"], probe=False)
    assert all(c[0] != "probe" for c in orch.calls)


@pytest.mark.asyncio
async def test_after_login_signed_in_starts_a_new_generation(monkeypatch, cleared):
    monkeypatch.setattr(after_login, "_refresh", lambda acct, server=None: True)
    monkeypatch.setattr(after_login.grants, "keys_for_server", lambda server: [f"{server}|k1"])
    marked: list = []

    async def mark(keys):
        marked.append(list(keys))
    monkeypatch.setattr(after_login.grant_health, "mark_signed_in", mark)
    await after_login.after_login(_Orch(), "notion", ["alice"], signed_in=True)
    assert marked == [["notion|k1"]]


@pytest.mark.asyncio
async def test_after_login_default_is_not_a_sign_in(monkeypatch, cleared):
    monkeypatch.setattr(after_login, "_refresh", lambda acct, server=None: True)

    async def mark(keys):
        pytest.fail("a heal is not a sign-in")
    monkeypatch.setattr(after_login.grant_health, "mark_signed_in", mark)
    await after_login.after_login(_Orch(), "notion", ["alice"])


@pytest.mark.asyncio
async def test_after_login_sign_in_mark_failure_still_reconnects(monkeypatch, cleared, caplog):
    monkeypatch.setattr(after_login, "_refresh", lambda acct, server=None: True)

    def boom(server):
        raise RuntimeError("scan failed")
    monkeypatch.setattr(after_login.grants, "keys_for_server", boom)
    orch = _Orch()
    out = await after_login.after_login(orch, "notion", ["alice"], signed_in=True)
    assert out["reconnected"] == 2
    assert any(r.levelname == "WARNING" for r in caplog.records)
