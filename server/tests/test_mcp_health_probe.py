import asyncio

import pytest

from server.mcp_health import probe
from server.tests.fakes.fake_sdk import FakeSDKClient


def _factory(answer, opened):
    def make(account, cfg_dir):
        opened.append(account)
        return FakeSDKClient([], mcp_status=answer, startup_frames=[])
    return make


class _Slow(FakeSDKClient):          # the real control request suspends; the fake would not
    async def get_mcp_status(self):
        await asyncio.sleep(0.02)
        return await super().get_mcp_status()


class _HangOnEnter(FakeSDKClient):
    async def __aenter__(self):
        await asyncio.sleep(3600)


def _hang_factory(account, cfg_dir):
    return _HangOnEnter([], mcp_status=[{"name": "a", "status": "connected"}], startup_frames=[])


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

    def f(account, cfg_dir):
        opened.append(account)
        return _Slow([], mcp_status=[{"name": "a", "status": "connected"}], startup_frames=[])

    await asyncio.gather(probe.probe_account("alice", client_factory=f), probe.probe_account("alice", client_factory=f))
    assert opened == ["alice"]


@pytest.mark.asyncio
async def test_cancelled_first_caller_does_not_fail_the_second(monkeypatch, tmp_path):
    monkeypatch.setattr(probe, "_cfg_dir_for", lambda a: tmp_path)

    def f(account, cfg_dir):
        return _Slow([], mcp_status=[{"name": "a", "status": "connected"}], startup_frames=[])

    first = asyncio.ensure_future(probe.probe_account("alice", client_factory=f))
    await asyncio.sleep(0)
    second = asyncio.ensure_future(probe.probe_account("alice", client_factory=f))
    await asyncio.sleep(0)
    first.cancel()
    states = await second
    assert [s.status for s in states] == ["connected"]


@pytest.mark.asyncio
async def test_probe_never_composes_or_writes_credentials(monkeypatch, tmp_path):
    import viberpit.configdir as cfgmod
    monkeypatch.setattr(cfgmod, "compose", lambda *a, **k: pytest.fail("probe must not compose"))
    (tmp_path / ".credentials.json").write_text('{"claudeAiOauth": {"x": 1}}')
    monkeypatch.setattr(probe, "_cfg_dir_for", lambda a: tmp_path)
    await probe.probe_account("alice", client_factory=_factory([{"name": "a", "status": "connected"}], []))
    assert (tmp_path / ".credentials.json").read_text() == '{"claudeAiOauth": {"x": 1}}'


@pytest.mark.asyncio
async def test_default_client_runs_on_scratch_mirror_not_the_real_cfg_dir(monkeypatch, tmp_path):
    """The CLI may rotate claudeAiOauth and rewrite .credentials.json: it must be pointed at a
    host-local mirror, never the (sshfs-shared) account dir, and must not hide user servers."""
    import claude_agent_sdk
    real = tmp_path / "cfg" / "alice"
    real.mkdir(parents=True)
    (real / ".claude.json").write_text('{"mcpServers": {}}')
    (real / ".credentials.json").write_text('{"claudeAiOauth": {"x": 1}}')
    monkeypatch.setenv("VIBERPIT_HELPER_SCRATCH_ROOT", str(tmp_path / "scratch"))
    monkeypatch.setenv("VIBERPIT_HELPER_SCRATCH_CONFIG", "true")
    seen = {}

    class _Cap(FakeSDKClient):
        def __init__(self, options=None, **_):
            super().__init__([], mcp_status=[{"name": "a", "status": "connected"}], startup_frames=[])
            seen["options"] = options

    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", _Cap)
    monkeypatch.setattr(probe, "_cfg_dir_for", lambda a: real)
    await probe.probe_account("alice")
    opts = seen["options"]
    assert opts.env["CLAUDE_CONFIG_DIR"] != str(real)
    assert opts.env["CLAUDE_CONFIG_DIR"].startswith(str(tmp_path / "scratch"))
    assert not getattr(opts, "strict_mcp_config", False)
    assert (real / ".credentials.json").read_text() == '{"claudeAiOauth": {"x": 1}}'


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


@pytest.mark.asyncio
async def test_hung_client_is_skipped_and_releases_everything(monkeypatch, tmp_path, isolated_db):
    monkeypatch.setattr(probe, "_cfg_dir_for", lambda a: tmp_path)
    out = await probe.probe(["alice"], client_factory=_hang_factory, deadline_s=0.1)
    assert out["skipped"] == [{"account": "alice", "reason": "timed out"}]
    assert probe._inflight == {}
    again = await probe.probe(["alice"], client_factory=_factory([{"name": "a", "status": "connected"}], []))
    assert again["skipped"] == [] and [r["status"] for r in again["rows"]] == ["connected"]


@pytest.mark.asyncio
async def test_account_without_a_slot_before_deadline_is_budget_exhausted(monkeypatch, tmp_path, isolated_db):
    monkeypatch.setattr(probe, "_cfg_dir_for", lambda a: tmp_path)
    monkeypatch.setattr(probe, "_MAX_CONCURRENT", 1)
    out = await probe.probe(["a1", "a2"], client_factory=_hang_factory, deadline_s=0.1)
    reasons = sorted(s["reason"] for s in out["skipped"])
    assert reasons == ["budget exhausted", "timed out"]


@pytest.mark.asyncio
async def test_store_failure_for_one_account_is_contained(monkeypatch, tmp_path, isolated_db):
    from server.mcp_health import store
    monkeypatch.setattr(probe, "_cfg_dir_for", lambda a: tmp_path)
    real = store.upsert_states

    async def flaky(account, *a, **k):
        if account == "bad":
            raise RuntimeError("db down")
        return await real(account, *a, **k)

    monkeypatch.setattr(store, "upsert_states", flaky)
    out = await probe.probe(["bad", "good"], client_factory=_factory([{"name": "a", "status": "connected"}], []))
    assert out["skipped"] == [{"account": "bad", "reason": "RuntimeError"}]
    assert {r["account"] for r in out["rows"]} == {"good"}
