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
    opened = []

    def hang(account, cfg_dir):
        opened.append(account)           # only the slot holder ever reaches the client factory
        return _HangOnEnter([], mcp_status=[{"name": "a", "status": "connected"}], startup_frames=[])

    # The budget covers the thread-run cfg lookup, so it must outlast a loaded worker's scheduling.
    out = await probe.probe(["a1", "a2"], client_factory=hang, deadline_s=1.5)
    assert len(opened) == 1
    holder = opened[0]
    waiter = ({"a1", "a2"} - {holder}).pop()
    # Whichever account won the only slot times out; the other never got one.
    assert {s["account"]: s["reason"] for s in out["skipped"]} == {holder: "timed out", waiter: "budget exhausted"}


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


# --- final-review fix wave -------------------------------------------------------------

@pytest.mark.asyncio
async def test_select_accounts_gates_on_helper_readiness(monkeypatch):
    from viberpit import accounts
    monkeypatch.setattr(accounts, "_anthropic_account_names", lambda: ["ready", "disabled"])
    monkeypatch.setattr(probe, "is_account_helper_ready", lambda n: n == "ready")
    names, skipped = await probe.select_accounts(None)
    assert names == ["ready"] and skipped == []          # implicit: not-ready accounts are never probed
    names, skipped = await probe.select_accounts("disabled")
    assert names == [] and skipped == [{"account": "disabled", "reason": "account not ready"}]


@pytest.mark.asyncio
async def test_orchestrator_probe_never_probes_a_not_ready_account(monkeypatch, isolated_db):
    from server.sessions.orchestrator import SessionOrchestrator
    from viberpit import accounts
    monkeypatch.setattr(accounts, "_anthropic_account_names", lambda: ["ready", "disabled"])
    monkeypatch.setattr(probe, "is_account_helper_ready", lambda n: n == "ready")
    seen = []

    async def fake_probe(names, **kw):
        seen.append(list(names))
        return {"rows": [], "skipped": []}

    monkeypatch.setattr(probe, "probe", fake_probe)
    out = await SessionOrchestrator.probe_mcp(object(), None)
    assert seen == [["ready"]] and out["skipped"] == []
    out = await SessionOrchestrator.probe_mcp(object(), "disabled")
    assert out["skipped"] == [{"account": "disabled", "reason": "account not ready"}]


@pytest.mark.asyncio
async def test_account_store_reads_go_through_account_store_io(monkeypatch, tmp_path):
    from viberpit import accounts
    calls = []
    real = probe.account_store_io

    async def spy(fn, *a, **k):
        calls.append(getattr(fn, "__name__", repr(fn)))
        return await real(fn, *a, **k)

    monkeypatch.setattr(probe, "account_store_io", spy)
    def _anthropic_account_names():
        return ["alice"]

    def is_account_helper_ready(n):
        return True

    def _cfg_dir_for(a):
        return tmp_path

    monkeypatch.setattr(accounts, "_anthropic_account_names", _anthropic_account_names)
    monkeypatch.setattr(probe, "is_account_helper_ready", is_account_helper_ready)
    monkeypatch.setattr(probe, "_cfg_dir_for", _cfg_dir_for)
    await probe.select_accounts(None)
    await probe.probe_account("alice", client_factory=_factory([{"name": "a", "status": "connected"}], []))
    assert "_anthropic_account_names" in calls and "is_account_helper_ready" in calls
    assert "_cfg_dir_for" in calls


@pytest.mark.asyncio
async def test_scratch_mirror_gets_current_shared_servers_real_dir_untouched(monkeypatch, tmp_path):
    import json
    import claude_agent_sdk
    from viberpit import paths
    real = tmp_path / "cfg" / "alice"
    real.mkdir(parents=True)
    (real / ".claude.json").write_text('{"mcpServers": {"old": {"command": "x"}}}')
    (real / ".credentials.json").write_text('{"claudeAiOauth": {"x": 1}}')
    before = {p.name: p.read_bytes() for p in real.iterdir()}
    shared = tmp_path / "claude.shared.json"
    shared.write_text('{"mcpServers": {"fresh": {"command": "y"}}}')
    monkeypatch.setattr(paths, "shared_claude_json_path", lambda: shared)
    monkeypatch.setenv("VIBERPIT_HELPER_SCRATCH_ROOT", str(tmp_path / "scratch"))
    monkeypatch.setenv("VIBERPIT_HELPER_SCRATCH_CONFIG", "true")
    seen = {}

    class _Cap(FakeSDKClient):
        def __init__(self, options=None, **_):
            super().__init__([], mcp_status=[{"name": "fresh", "status": "connected"}], startup_frames=[])
            seen["scratch"] = options.env["CLAUDE_CONFIG_DIR"]
            seen["servers"] = json.loads((__import__("pathlib").Path(seen["scratch"]) / ".claude.json").read_text())["mcpServers"]

    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", _Cap)
    monkeypatch.setattr(probe, "_cfg_dir_for", lambda a: real)
    await probe.probe_account("alice")
    assert set(seen["servers"]) == {"fresh"}
    assert {p.name: p.read_bytes() for p in real.iterdir()} == before


@pytest.mark.asyncio
async def test_probe_overlays_the_scratch_mirrors_mcp_oauth(monkeypatch, tmp_path):
    """The probe reads mcpOAuth from the scratch mirror the client runs on (its
    CLAUDE_CONFIG_DIR), not the real cfg dir: an expired token reports needs-auth."""
    import json
    from types import SimpleNamespace
    real, scratch = tmp_path / "real", tmp_path / "scratch"
    real.mkdir()
    scratch.mkdir()
    (scratch / ".credentials.json").write_text(json.dumps({"mcpOAuth": {"aws|1": {
        "serverName": "aws", "accessToken": "t", "expiresAt": 1}}}))
    monkeypatch.setattr(probe, "_cfg_dir_for", lambda a: real)

    def make(account, cfg_dir):
        c = FakeSDKClient([], mcp_status=[{"name": "aws", "status": "connected"},
                                          {"name": "gh", "status": "connected"}], startup_frames=[])
        c.options = SimpleNamespace(env={"CLAUDE_CONFIG_DIR": str(scratch)})
        return c

    states = await probe.probe_account("alice", client_factory=make)
    by = {s.name: s for s in states}
    assert (by["aws"].status, by["aws"].auth) == ("needs-auth", "oauth")
    assert by["aws"].error.startswith("Sign-in expired")
    assert (by["gh"].status, by["gh"].auth) == ("connected", None)
