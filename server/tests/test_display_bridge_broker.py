"""Edge display-bridge broker (spec 2026-11-25-per-host-display-target §2 "Lifecycle",
"Spawn waits for the bridge"): the edge tells each target daemon, as desired state, which
source hosts should have a live bridge onto its desktop."""
from __future__ import annotations

import asyncio
import time

import pytest

from server.db import db_session
from server.db.models import Host, Session as DbSession
from server.sessions.display_bridge_broker import DisplayBridgeBroker
from server.sessions.fleet_orchestrator import FleetOrchestrator

pytestmark = pytest.mark.asyncio


class _FakeTarget:
    """A target daemon's ``display_bridge_ensure`` RPC (via RemoteOrchestrator)."""

    def __init__(self, state="up", *, fail: Exception | None = None):
        self.state = state
        self.fail = fail
        self.calls: list[tuple[list[str], dict[str, str]]] = []

    async def display_bridge_ensure(self, sources, ssh_dests, *, timeout=None):
        self.calls.append((list(sources), dict(ssh_dests)))
        if self.fail is not None:
            raise self.fail
        return [{"source": s, "state": self.state, "legs": [], "since": None}
                for s in sources]


async def _seed(*rows):
    async with db_session() as db:
        for r in rows:
            db.add(r)


def _host(name, *, enabled=True, target=None, ssh=None, dssh=None, bridge=True):
    return Host(name=name, endpoint=f"https://{name}:9443", enabled=enabled,
                ssh_host=ssh, display_ssh_host=dssh, display_target=target,
                display_bridge={"accepts": [], "active": []} if bridge else None)


def _remote_session(sid, source, target, status="idle"):
    return DbSession(id=sid, name=sid, account_name="a", cwd="/tmp", status=status, host=source,
                     display_mode="remote", display_target_host=target)


def _broker(clients: dict, timeout_s=2.0):
    return DisplayBridgeBroker(clients=clients.get, timeout_s=timeout_s)


async def test_prepare_returns_target_when_up(isolated_db):
    await _seed(_host("rocky", target="host:dobby", ssh="tony@rocky"),
                _host("dobby"))
    dobby = _FakeTarget("up")
    assert await _broker({"dobby": dobby}).prepare("rocky") == "dobby"
    assert dobby.calls[0] == (["rocky"], {"rocky": "tony@rocky"})


async def test_prepare_times_out_returns_none_within_timeout(isolated_db):
    await _seed(_host("rocky", target="host:dobby", ssh="tony@rocky"), _host("dobby"))
    dobby = _FakeTarget("starting")
    t0 = time.monotonic()
    assert await _broker({"dobby": dobby}, timeout_s=0.3).prepare("rocky") is None
    assert time.monotonic() - t0 < 1.5
    assert len(dobby.calls) >= 1


async def test_prepare_uses_display_ssh_host_over_ssh_host(isolated_db):
    await _seed(_host("rocky", target="host:dobby", ssh="ops@rocky", dssh="tony@rocky"),
                _host("dobby"))
    dobby = _FakeTarget("up")
    await _broker({"dobby": dobby}).prepare("rocky")
    assert dobby.calls[0][1] == {"rocky": "tony@rocky"}


async def test_prepare_without_ssh_destination_never_calls_ensure(isolated_db):
    await _seed(_host("rocky", target="host:dobby"), _host("dobby"))
    dobby = _FakeTarget("up")
    assert await _broker({"dobby": dobby}).prepare("rocky") is None
    assert dobby.calls == []


async def test_prepare_returns_none_at_once_when_target_refuses(isolated_db):
    await _seed(_host("rocky", target="host:dobby", ssh="tony@rocky"), _host("dobby"))
    dobby = _FakeTarget("not_accepted")
    t0 = time.monotonic()
    assert await _broker({"dobby": dobby}, timeout_s=5.0).prepare("rocky") is None
    assert time.monotonic() - t0 < 1.0


async def test_prepare_swallows_an_unreachable_target(isolated_db):
    from server.ipc.client import DaemonUnavailable
    await _seed(_host("rocky", target="host:dobby", ssh="tony@rocky"), _host("dobby"))
    dobby = _FakeTarget(fail=DaemonUnavailable("All connection attempts failed"))
    assert await _broker({"dobby": dobby}, timeout_s=0.3).prepare("rocky") is None


class _Rec:
    def __init__(self, id):
        self.id = id
        self.host = None


class _SourceOrch:
    def __init__(self, name):
        self.name = name
        self.created: list[dict] = []
        self.archived: list[str] = []

    async def create_session(self, **kw):
        self.created.append(kw)
        return _Rec(f"sess-{self.name}")

    async def archive_session(self, session_id, *a, **k):
        self.archived.append(session_id)


class _Router:
    def __init__(self, orchs, order):
        self._orchs, self._order = orchs, order

    def get(self, name):
        return self._orchs[name]

    def names(self):
        return sorted(self._orchs)

    async def pick(self, **_kw):
        return list(self._order)


def _fleet(monkeypatch, broker, source="rocky"):
    orch = _SourceOrch(source)
    fo = FleetOrchestrator(_Router({source: orch}, [source]), primary=source,
                           display_bridge=broker)

    async def _stamp(sid, host):
        return None
    monkeypatch.setattr(fo, "_stamp_host", _stamp)
    return fo, orch


async def test_create_session_passes_target_after_bridge_is_up(isolated_db, monkeypatch):
    await _seed(_host("rocky", target="host:dobby", ssh="tony@rocky"), _host("dobby"))
    dobby = _FakeTarget("up")
    fo, rocky = _fleet(monkeypatch, _broker({"dobby": dobby}))
    await fo.create_session(account="a")
    assert rocky.created[0]["display_target"] == "dobby"
    assert dobby.calls and dobby.calls[0][0] == ["rocky"]


async def test_create_session_passes_target_even_when_prepare_timed_out(
        isolated_db, monkeypatch):
    await _seed(_host("rocky", target="host:dobby", ssh="tony@rocky"), _host("dobby"))
    fo, rocky = _fleet(monkeypatch, _broker({"dobby": _FakeTarget("starting")}, timeout_s=0.2))
    await fo.create_session(account="a")
    assert rocky.created[0]["display_target"] == "dobby"


async def test_target_host_disabled_falls_back_virtual(isolated_db, monkeypatch):
    """A disabled target is never asked to bridge and the spawn does not wait; the source
    daemon still gets the target, and its own liveness check (no sockets) records the
    precise fallback reason and makes the session virtual."""
    await _seed(_host("rocky", target="host:dobby", ssh="tony@rocky"),
                _host("dobby", enabled=False))
    dobby = _FakeTarget("up")
    fo, rocky = _fleet(monkeypatch, _broker({"dobby": dobby}, timeout_s=5.0))
    t0 = time.monotonic()
    await fo.create_session(account="a")
    assert time.monotonic() - t0 < 1.0
    assert dobby.calls == []
    assert rocky.created[0]["display_target"] == "dobby"


async def test_create_session_local_host_sends_no_target(isolated_db, monkeypatch):
    await _seed(_host("rocky", ssh="tony@rocky"), _host("dobby"))
    dobby = _FakeTarget("up")
    fo, rocky = _fleet(monkeypatch, _broker({"dobby": dobby}))
    await fo.create_session(account="a")
    assert "display_target" not in rocky.created[0]
    assert dobby.calls == []


async def test_explicit_virtual_request_skips_the_bridge(isolated_db, monkeypatch):
    await _seed(_host("rocky", target="host:dobby", ssh="tony@rocky"), _host("dobby"))
    dobby = _FakeTarget("up")
    fo, rocky = _fleet(monkeypatch, _broker({"dobby": dobby}))
    await fo.create_session(account="a", display="virtual")
    assert "display_target" not in rocky.created[0]
    assert dobby.calls == []


async def test_queue_only_create_passes_target_without_waiting(isolated_db, monkeypatch):
    await _seed(_host("rocky", target="host:dobby", ssh="tony@rocky"), _host("dobby"))
    dobby = _FakeTarget("starting")
    fo, rocky = _fleet(monkeypatch, _broker({"dobby": dobby}, timeout_s=5.0))
    t0 = time.monotonic()
    await fo._dispatch(["rocky"], {"account": "a", "queue_only": True})
    assert time.monotonic() - t0 < 1.0
    assert rocky.created[0]["display_target"] == "dobby"
    assert dobby.calls == []


async def test_desired_groups_live_remote_sessions_by_target(isolated_db):
    await _seed(_host("rocky", target="host:dobby", ssh="r"), _host("pi", ssh="p"),
                _host("dobby"), _host("mac"),
                _remote_session("s1", "rocky", "dobby"),
                _remote_session("s2", "rocky", "dobby", status="in_progress"),
                _remote_session("s3", "pi", "dobby"),
                _remote_session("s4", "pi", "mac"),
                _remote_session("gone", "pi", "rocky", status="archived"),
                _remote_session("ended", "pi", "rocky", status="ended"),
                DbSession(id="v", name="v", account_name="a", cwd="/tmp", status="idle", host="pi",
                          display_mode="virtual"))
    assert await _broker({}).desired() == {"dobby": ["pi", "rocky"], "mac": ["pi"]}


async def test_desired_includes_in_flight_prepares(isolated_db):
    await _seed(_host("rocky", target="host:dobby", ssh="tony@rocky"), _host("dobby"))
    seen: list[dict] = []

    class _Slow(_FakeTarget):
        async def display_bridge_ensure(self, sources, ssh_dests, *, timeout=None):
            seen.append(await broker.desired())
            return [{"source": s, "state": "up"} for s in sources]

    broker = _broker({"dobby": _Slow()})
    assert await broker.prepare("rocky") == "dobby"
    assert seen == [{"dobby": ["rocky"]}]
    assert await broker.desired() == {}


async def test_reassert_sends_empty_list_to_target_with_no_sessions(isolated_db):
    await _seed(_host("rocky", target="host:dobby", ssh="tony@rocky"), _host("dobby"),
                _host("mac", bridge=False))
    dobby, rocky, mac = _FakeTarget(), _FakeTarget(), _FakeTarget()
    await _broker({"dobby": dobby, "rocky": rocky, "mac": mac}).reassert()
    # dobby is the configured target of rocky; rocky advertises a bridge. mac advertises
    # none and is nobody's target, so it is not asked.
    assert dobby.calls == [([], {})]
    assert rocky.calls == [([], {})]
    assert mac.calls == []


async def test_reassert_sends_each_target_its_desired_sources(isolated_db):
    await _seed(_host("rocky", target="host:dobby", ssh="tony@rocky"), _host("dobby"),
                _remote_session("s1", "rocky", "dobby"))
    dobby = _FakeTarget()
    await _broker({"dobby": dobby}).reassert()
    assert dobby.calls == [(["rocky"], {"rocky": "tony@rocky"})]


async def test_reassert_survives_an_unreachable_target(isolated_db, caplog):
    import logging

    from server.ipc.client import DaemonUnavailable
    await _seed(_host("rocky", target="host:dobby", ssh="tony@rocky"), _host("dobby"),
                _host("pi"))
    dobby = _FakeTarget(fail=DaemonUnavailable("All connection attempts failed"))
    pi = _FakeTarget()
    with caplog.at_level(logging.INFO):
        await _broker({"dobby": dobby, "pi": pi}).reassert()
    assert pi.calls == [([], {})]
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


async def test_reconcile_reasserts_desired_set_after_restart(isolated_db):
    await _seed(_host("rocky", target="host:dobby", ssh="tony@rocky"), _host("dobby"),
                _remote_session("s1", "rocky", "dobby"))
    first, second = _FakeTarget(), _FakeTarget()
    await _broker({"dobby": first}).reassert()
    # An edge restart: a brand-new broker over the same DB asserts the same set.
    await _broker({"dobby": second}).reassert()
    assert first.calls == second.calls == [(["rocky"], {"rocky": "tony@rocky"})]


async def test_kill_switch_off_never_calls_ensure(isolated_db, monkeypatch):
    monkeypatch.setenv("VIBERPIT_DISPLAY_BRIDGE_ENABLED", "false")
    await _seed(_host("rocky", target="host:dobby", ssh="tony@rocky"), _host("dobby"),
                _remote_session("s1", "rocky", "dobby"))
    dobby = _FakeTarget()
    broker = _broker({"dobby": dobby})
    await broker.reassert()
    assert await broker.prepare("rocky") is None
    fo, rocky = _fleet(monkeypatch, broker)
    await fo.create_session(account="a")
    assert dobby.calls == []
    assert "display_target" not in rocky.created[0]


async def test_archive_reasserts_the_desired_set(isolated_db, monkeypatch):
    await _seed(_host("rocky", target="host:dobby", ssh="tony@rocky"), _host("dobby"),
                _remote_session("s1", "rocky", "dobby"))
    dobby = _FakeTarget()
    fo, rocky = _fleet(monkeypatch, _broker({"dobby": dobby}))
    fo._host_cache["s1"] = "rocky"
    async with db_session() as db:
        (await db.get(DbSession, "s1")).status = "archived"
    await fo.archive_session("s1")
    await fo.drain_display_reasserts()
    assert rocky.archived == ["s1"]
    assert dobby.calls == [([], {})]


async def test_probe_loop_reasserts_every_tick(monkeypatch):
    from server.sessions import host_probe

    async def _probe_all(**_kw):
        return 0
    monkeypatch.setattr(host_probe, "probe_all", _probe_all)
    stop = asyncio.Event()
    ticks = []

    class _B:
        async def reassert(self):
            ticks.append(1)
            if len(ticks) == 2:
                stop.set()

    await asyncio.wait_for(
        host_probe.probe_loop(0.01, stop=stop, display_bridge=_B()), timeout=5)
    assert len(ticks) == 2
