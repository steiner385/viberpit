import asyncio

import pytest

from server.mcp_health import probe
from server.tests.fakes.fake_sdk import FakeSDKClient


def _factory(answer, opened):
    def make(account, cfg_dir):
        opened.append(account)
        return FakeSDKClient([], mcp_status=answer, startup_frames=[])
    return make


@pytest.mark.asyncio
async def test_probe_settles_pending(monkeypatch, tmp_path):
    answers = iter([[{"name": "a", "status": "pending"}], [{"name": "a", "status": "connected"}]])
    monkeypatch.setattr(probe, "_cfg_dir_for", lambda a: tmp_path)
    states = await probe.probe_account("alice", client_factory=_factory(lambda: next(answers), []), interval_s=0.01)
    assert [s.status for s in states] == ["connected"]


@pytest.mark.asyncio
async def test_probe_timeout_records_pending_with_error(monkeypatch, tmp_path):
    monkeypatch.setattr(probe, "_cfg_dir_for", lambda a: tmp_path)
    states = await probe.probe_account("alice", client_factory=_factory([{"name": "a", "status": "pending"}], []),
                                       settle_s=0.05, interval_s=0.01)
    assert states[0].status == "pending" and "timed out" in states[0].error


@pytest.mark.asyncio
async def test_concurrent_probes_of_one_account_share_one_client(monkeypatch, tmp_path):
    monkeypatch.setattr(probe, "_cfg_dir_for", lambda a: tmp_path)
    opened = []

    class _Slow(FakeSDKClient):          # the real control request suspends; the fake would not
        async def get_mcp_status(self):
            await asyncio.sleep(0.02)
            return await super().get_mcp_status()

    def f(account, cfg_dir):
        opened.append(account)
        return _Slow([], mcp_status=[{"name": "a", "status": "connected"}], startup_frames=[])

    await asyncio.gather(probe.probe_account("alice", client_factory=f), probe.probe_account("alice", client_factory=f))
    assert opened == ["alice"]


@pytest.mark.asyncio
async def test_probe_never_composes_or_writes_credentials(monkeypatch, tmp_path):
    import viberpit.configdir as cfgmod
    monkeypatch.setattr(cfgmod, "compose", lambda *a, **k: pytest.fail("probe must not compose"))
    (tmp_path / ".credentials.json").write_text('{"claudeAiOauth": {"x": 1}}')
    monkeypatch.setattr(probe, "_cfg_dir_for", lambda a: tmp_path)
    await probe.probe_account("alice", client_factory=_factory([{"name": "a", "status": "connected"}], []))
    assert (tmp_path / ".credentials.json").read_text() == '{"claudeAiOauth": {"x": 1}}'


@pytest.mark.asyncio
async def test_probe_skips_accounts_without_cfg(monkeypatch, isolated_db):
    monkeypatch.setattr(probe, "_cfg_dir_for", lambda a: None)
    out = await probe.probe(["ghost"], client_factory=_factory([], []))
    assert out["skipped"] == [{"account": "ghost", "reason": "no config dir on this host"}]


@pytest.mark.asyncio
async def test_probe_persists_rows_for_this_host(monkeypatch, tmp_path, isolated_db):
    from server.hostname import self_host_name
    monkeypatch.setattr(probe, "_cfg_dir_for", lambda a: tmp_path)
    out = await probe.probe(["alice"], client_factory=_factory([{"name": "a", "status": "connected"}], []))
    assert out["skipped"] == []
    assert [(r["account"], r["host"], r["server"], r["status"], r["source"]) for r in out["rows"]] == [
        ("alice", self_host_name(), "a", "connected", "probe")]
