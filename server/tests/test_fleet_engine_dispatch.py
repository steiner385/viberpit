"""Engine-aware candidate filtering and the pre-dispatch gate (plan task 5.1).

The scenarios here are the ones the plan names explicitly: mixed-version rollout,
missing binary, schema mismatch, host preference, all-incompatible, and concurrent
same-account dispatch. They live together because they are one behaviour seen from
three layers — the pure ranker, the registry-backed router, and the live gate — and
splitting them across three files is how the three drift apart.
"""
from __future__ import annotations

import pytest

from ccswitch.engines import AgentEngine
from server.db import db_session, init_db
from server.db.models import Host
from server.sessions.engine import AgentEngineUnavailable
from server.sessions.fleet import HostCandidate, engine_rejection, rank_hosts
from server.sessions.fleet_router import (
    FleetRouter,
    NoEngineHostAvailable,
    NoHostAvailable,
)
from server.sessions.host_engines import parse_engines_info

CODEX_READY = {
    "claude_code": {"available": True},
    "codex": {"available": True, "cliVersion": "codex-cli 0.153.2",
              "contractSha256": "pinned"},
}
CODEX_NO_BINARY = {
    "claude_code": {"available": True},
    "codex": {"available": False, "reason": "binary_not_found"},
}
CODEX_OTHER_CONTRACT = {
    "claude_code": {"available": True},
    "codex": {"available": True, "cliVersion": "codex-cli 0.200.0",
              "contractSha256": "moved"},
}


def _candidate(name: str, engines: dict | None, **kw) -> HostCandidate:
    return HostCandidate(
        name=name,
        enabled=kw.pop("enabled", True),
        healthy=kw.pop("healthy", True),
        weight=kw.pop("weight", 100),
        active_sessions=kw.pop("active_sessions", 0),
        max_sessions=kw.pop("max_sessions", None),
        load_avg=kw.pop("load_avg", None),
        mem_available_mb=kw.pop("mem_available_mb", None),
        engines=parse_engines_info(engines),
    )


# --- the pure ranker ----------------------------------------------------------

def test_ranking_without_an_engine_is_unchanged():
    """Every pre-existing caller keeps its behaviour; the filter is opt-in."""
    hosts = [_candidate("dobby", CODEX_READY), _candidate("rocky", None)]
    assert sorted(rank_hosts(hosts)) == ["dobby", "rocky"]


def test_a_legacy_runner_is_dropped_from_a_codex_ranking_but_keeps_claude():
    """The mixed-version rollout window, from the ranker's side."""
    hosts = [_candidate("dobby", CODEX_READY), _candidate("rocky", None)]
    assert rank_hosts(hosts, engine=AgentEngine.CODEX) == ["dobby"]
    assert sorted(rank_hosts(hosts, engine=AgentEngine.CLAUDE_CODE)) == ["dobby", "rocky"]


def test_a_missing_binary_removes_only_the_codex_candidacy():
    hosts = [_candidate("rocky", CODEX_NO_BINARY)]
    assert rank_hosts(hosts, engine=AgentEngine.CODEX) == []
    assert rank_hosts(hosts, engine=AgentEngine.CLAUDE_CODE) == ["rocky"]
    assert engine_rejection(hosts[0], AgentEngine.CODEX) == "binary_not_found"


def test_a_schema_mismatch_removes_a_runner_that_believes_it_is_fine():
    host = _candidate("rocky", CODEX_OTHER_CONTRACT)
    assert rank_hosts(
        [host], engine=AgentEngine.CODEX, expected_contract_sha256="pinned") == []
    assert engine_rejection(
        host, AgentEngine.CODEX,
        expected_contract_sha256="pinned") == "contract_hash_mismatch"


def test_the_engine_filter_never_overrides_health_or_capacity():
    """A capable but full/unhealthy host stays excluded — the filter only narrows."""
    hosts = [
        _candidate("full", CODEX_READY, active_sessions=4, max_sessions=4),
        _candidate("down", CODEX_READY, healthy=False),
        _candidate("off", CODEX_READY, enabled=False),
        _candidate("unprobed", CODEX_READY, healthy=None),
    ]
    assert rank_hosts(hosts, engine=AgentEngine.CODEX) == []


def test_engine_capability_does_not_reorder_capable_hosts():
    """Capability is a filter, not a score: ranking among the survivors is untouched."""
    hosts = [
        _candidate("weak", CODEX_READY, weight=1),
        _candidate("strong", CODEX_READY, weight=1000),
    ]
    assert rank_hosts(hosts, engine=AgentEngine.CODEX) == ["strong", "weak"]


# --- the registry-backed router ----------------------------------------------

async def _seed(tmp_path, dbname: str, rows: list[tuple[str, dict | None]]) -> FleetRouter:
    """A one-file registry seeded with the given hosts, mirroring test_fleet_router."""
    await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / dbname}")
    async with db_session() as session:
        for name, engines in rows:
            session.add(Host(
                name=name, endpoint=f"https://{name}:9443", enabled=True,
                healthy=True, weight=100, active_sessions=0, engines=engines))
    router = FleetRouter()
    for name, _ in rows:
        router.add(name, object())
    return router


@pytest.mark.asyncio
async def test_pick_returns_only_the_codex_capable_runner(tmp_path):
    router = await _seed(tmp_path, "e1.db", [("dobby", CODEX_READY), ("rocky", CODEX_NO_BINARY)])
    assert await router.pick(
        engine=AgentEngine.CODEX, expected_contract_sha256="pinned") == ["dobby"]


@pytest.mark.asyncio
async def test_preference_is_a_preference_not_permission(tmp_path):
    """`prefer` must not promote a host that cannot run the requested engine.

    The plan calls this out by name. Silently honouring the preference would turn a
    filtered dispatch back into the gamble the filter exists to remove.
    """
    router = await _seed(tmp_path, "e2.db", [("dobby", CODEX_READY), ("rocky", CODEX_NO_BINARY)])
    assert await router.pick(
        prefer="rocky", engine=AgentEngine.CODEX,
        expected_contract_sha256="pinned") == ["dobby"]
    # ...and it IS honoured when the host is actually capable.
    assert await router.pick(
        prefer="dobby", engine=AgentEngine.CODEX,
        expected_contract_sha256="pinned") == ["dobby"]


@pytest.mark.asyncio
async def test_preference_still_wins_for_an_engine_every_host_can_run(tmp_path):
    router = await _seed(tmp_path, "e3.db", [("dobby", CODEX_READY), ("rocky", CODEX_NO_BINARY)])
    assert (await router.pick(prefer="rocky"))[0] == "rocky"


@pytest.mark.asyncio
async def test_all_incompatible_names_the_engine_and_every_reason(tmp_path):
    """The operator must learn 'install codex', not 'your fleet is down'."""
    router = await _seed(tmp_path, "e4.db", [("dobby", CODEX_NO_BINARY), ("rocky", None)])
    with pytest.raises(NoEngineHostAvailable) as exc:
        await router.pick(engine=AgentEngine.CODEX, expected_contract_sha256="pinned")
    assert exc.value.engine is AgentEngine.CODEX
    assert exc.value.reasons == {
        "dobby": "binary_not_found", "rocky": "engine_not_advertised"}
    # Still a NoHostAvailable, so every existing handler keeps catching it.
    assert isinstance(exc.value, NoHostAvailable)


@pytest.mark.asyncio
async def test_an_unhealthy_fleet_is_reported_as_unhealthy_not_incompatible(tmp_path):
    """A down host is a health problem even when the request named an engine."""
    router = await _seed(tmp_path, "e5.db", [("dobby", CODEX_READY)])
    async with db_session() as session:
        host = await session.get(Host, "dobby")
        host.healthy = False
    with pytest.raises(NoHostAvailable) as exc:
        await router.pick(engine=AgentEngine.CODEX, expected_contract_sha256="pinned")
    assert not isinstance(exc.value, NoEngineHostAvailable)


@pytest.mark.asyncio
async def test_the_same_account_may_be_ranked_onto_several_runners(tmp_path):
    """Decision 5: no account affinity or lease is introduced by engine routing.

    Two concurrent launches for one OpenAI account must both see the full capable
    candidate list — the plan forbids adding an affinity that would serialize them.
    """
    router = await _seed(tmp_path, "e6.db", [("dobby", CODEX_READY), ("rocky", CODEX_READY)])
    first = await router.pick(
        engine=AgentEngine.CODEX, expected_contract_sha256="pinned")
    second = await router.pick(
        engine=AgentEngine.CODEX, expected_contract_sha256="pinned")
    assert sorted(first) == sorted(second) == ["dobby", "rocky"]


# --- the live pre-dispatch gate ----------------------------------------------

class _Client:
    """A daemon client that answers `/info` and records create_session calls."""

    def __init__(self, info: dict | Exception) -> None:
        self._info = info
        self.info_calls = 0
        self.created: list[dict] = []

    async def info(self) -> dict:
        self.info_calls += 1
        if isinstance(self._info, Exception):
            raise self._info
        return self._info

    async def call(self, method: str, **kwargs):
        # Record and stop: what is under test is WHETHER the daemon is called and
        # with which keys, not the record it would deserialize back.
        self.created.append({"method": method, **kwargs})
        raise _Dispatched()


class _Dispatched(Exception):
    """Sentinel: the RPC was reached."""


def _remote(info):
    from server.sessions.remote import RemoteOrchestrator

    client = _Client(info)
    return RemoteOrchestrator(client), client


@pytest.mark.asyncio
async def test_the_live_gate_refuses_a_daemon_that_lost_its_binary():
    """The registry ranked this host; the daemon itself says no. The daemon wins."""
    orch, client = _remote({"engines": CODEX_NO_BINARY})
    with pytest.raises(AgentEngineUnavailable):
        await orch.create_session(engine="codex", account="a", cwd="/tmp")
    assert client.created == []


@pytest.mark.asyncio
async def test_the_live_gate_refuses_a_legacy_daemon():
    orch, client = _remote({"sha": "abc", "protocol": 1})
    with pytest.raises(AgentEngineUnavailable):
        await orch.create_session(engine="codex", account="a", cwd="/tmp")
    assert client.created == []


@pytest.mark.asyncio
async def test_an_unreachable_daemon_is_refused_rather_than_assumed_capable():
    orch, client = _remote(OSError("connection refused"))
    with pytest.raises(AgentEngineUnavailable):
        await orch.create_session(engine="codex", account="a", cwd="/tmp")
    assert client.created == []


@pytest.mark.asyncio
async def test_a_capable_daemon_receives_the_engine_key():
    from server.codex.schema import PINNED_CONTRACT_SHA256

    orch, client = _remote({"engines": {
        "claude_code": {"available": True},
        "codex": {"available": True, "contractSha256": PINNED_CONTRACT_SHA256},
    }})
    with pytest.raises(_Dispatched):
        await orch.create_session(engine="codex", account="a", cwd="/tmp")
    assert client.created[0]["engine"] == "codex"


@pytest.mark.asyncio
async def test_claude_never_pays_for_the_engine_round_trip():
    """The gate must not add an RPC to the path every existing launch takes."""
    orch, client = _remote({"engines": CODEX_READY})
    with pytest.raises(_Dispatched):
        await orch.create_session(engine="claude_code", account="a", cwd="/tmp")
    assert client.info_calls == 0
    assert "engine" not in client.created[0]


@pytest.mark.asyncio
async def test_the_engine_answer_is_cached_across_a_burst():
    from server.codex.schema import PINNED_CONTRACT_SHA256

    orch, client = _remote({"engines": {
        "claude_code": {"available": True},
        "codex": {"available": True, "contractSha256": PINNED_CONTRACT_SHA256},
    }})
    for _ in range(3):
        with pytest.raises(_Dispatched):
            await orch.create_session(engine="codex", account="a", cwd="/tmp")
    assert client.info_calls == 1
