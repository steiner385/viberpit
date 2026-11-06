"""Fleet-wide live plugin reload fan-out (#3314)."""
from __future__ import annotations

import asyncio
from typing import Any, cast

from server.ipc.client import DaemonError, DaemonTimeout, DaemonUnavailable
from server.sessions.fleet_orchestrator import FleetOrchestrator


class _HostOrch:
    def __init__(self, behaviour: Any) -> None:
        self.behaviour = behaviour
        self.calls: list[bool] = []

    async def reload_plugins_live(self, hold_on_cache_impact: bool = True) -> Any:
        self.calls.append(hold_on_cache_impact)
        if isinstance(self.behaviour, BaseException):
            raise self.behaviour
        return self.behaviour() if callable(self.behaviour) else self.behaviour


class _Router:
    def __init__(self, hosts: dict[str, _HostOrch], reachable: list[str]) -> None:
        self.hosts, self.reachable = hosts, reachable

    def names(self) -> list[str]:
        return list(self.hosts)

    def get(self, name: str) -> _HostOrch:
        return self.hosts[name]

    async def reachable_names(self, *, always: str | None = None) -> list[str]:
        return self.reachable


def _fleet(hosts: dict[str, _HostOrch], reachable: list[str]) -> FleetOrchestrator:
    return FleetOrchestrator(cast(Any, _Router(hosts, reachable)), "dobby")


def test_reload_everywhere_labels_dead_and_old_hosts() -> None:
    hosts = {
        "dobby": _HostOrch([{"sessionId": "s1", "name": "n", "host": "dobby", "outcome": "applied"}]),
        "rocky": _HostOrch(DaemonUnavailable("connection refused")),
        "old": _HostOrch(DaemonError("UnknownMethod", "reload_plugins_live")),
        "slow": _HostOrch(DaemonTimeout("read timed out")),
        "gone": _HostOrch([]),
    }
    fleet = _fleet(hosts, ["dobby", "rocky", "old", "slow"])
    rows = asyncio.run(fleet.reload_plugins_everywhere(hold_on_cache_impact=False))
    assert {(r["host"], r["outcome"], r.get("failureCode")) for r in rows} == {
        ("dobby", "applied", None), ("rocky", "failed", "host_unreachable"),
        ("old", "failed", "daemon_outdated"), ("slow", "failed", "host_timeout"),
        ("gone", "failed", "host_unreachable")}
    assert len(rows) == 5
    assert all("sessionId" not in r for r in rows if r["outcome"] == "failed")
    assert hosts["gone"].calls == []  # known-down host is reported, not dialled
    assert hosts["dobby"].calls == [False]


def test_reload_everywhere_rejects_a_non_list_reply() -> None:
    fleet = _fleet({"dobby": _HostOrch({"x": 1})}, ["dobby"])
    rows = asyncio.run(fleet.reload_plugins_everywhere())
    assert rows == [{"host": "dobby", "outcome": "failed", "failureCode": "unparseable_result",
                     "message": "Malformed host reply."}]


def test_reload_everywhere_isolates_an_unexpected_error() -> None:
    fleet = _fleet({"dobby": _HostOrch([{"sessionId": "s", "host": "dobby", "outcome": "held"}]),
                    "odd": _HostOrch(RuntimeError("bug"))}, ["dobby", "odd"])
    rows = asyncio.run(fleet.reload_plugins_everywhere())
    assert {(r["host"], r["outcome"], r.get("failureCode")) for r in rows} == {
        ("dobby", "held", None), ("odd", "failed", "error_internal")}


def test_reload_everywhere_maps_each_non_dict_row_to_one_failed_row() -> None:
    good = {"sessionId": "s", "name": "n", "host": "dobby", "outcome": "applied"}
    fleet = _fleet({"dobby": _HostOrch([good, "junk", 3])}, ["dobby"])
    rows = asyncio.run(fleet.reload_plugins_everywhere())
    bad = {"host": "dobby", "outcome": "failed", "failureCode": "unparseable_result",
           "message": "Malformed host reply."}
    assert rows == [good, bad, bad]


def test_every_host_proxy_answers_the_reload_fan_out() -> None:
    """The fleet dials ``reload_plugins_live`` on each host proxy and the route calls
    ``reload_plugins_everywhere`` on whatever ``app.state.sessions`` is: every real class
    must carry both, or the reload silently never reaches that deployment shape."""
    from server.sessions.facade import InProcessOrchestratorAdapter
    from server.sessions.orchestrator import SessionOrchestrator
    from server.sessions.remote import RemoteOrchestrator
    for cls in (RemoteOrchestrator, InProcessOrchestratorAdapter):
        assert hasattr(cls, "reload_plugins_live"), cls
        assert hasattr(cls, "reload_plugins_everywhere"), cls
    assert hasattr(FleetOrchestrator, "reload_plugins_everywhere")
    assert hasattr(SessionOrchestrator, "reload_plugins_live")
