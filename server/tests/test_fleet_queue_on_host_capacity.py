"""Creating a session is never blocked by host capacity.

When every enabled, healthy host is at ``max_sessions`` the edge used to answer
``NoHostAvailable`` ("no enabled, healthy host with capacity; refusing to run
in-process ...") and the UI showed an error. The split-brain half of that refusal
is right — the edge must never run a session in-process — but the capacity half
is not: the daemon already knows how to persist a ``queued`` row for the global
cap, so a full fleet is a queueing condition, not a refusal.

The edge therefore turns a capacity-only refusal into a ``queue_only`` create on a
reachable daemon (the primary first), and a periodic nudge asks every host with
headroom to drain so a queued session starts when ANY host frees a slot.
"""
from __future__ import annotations

import types
from typing import cast

import pytest

from server.ipc.client import DaemonUnavailable
from server.sessions.engine import AgentEngine
from server.sessions.fleet_orchestrator import FleetOrchestrator
from server.sessions.fleet_router import FleetRouter, NoEngineHostAvailable, NoHostAvailable


class _Orch:
    """A per-host daemon stand-in recording create/drain calls."""

    def __init__(self, name: str, *, create_exc: Exception | None = None,
                 drain_exc: Exception | None = None) -> None:
        self.name = name
        self.creates: list[dict] = []
        self.drains = 0
        self.create_exc = create_exc
        self.drain_exc = drain_exc

    async def create_session(self, **kwargs):
        self.creates.append(dict(kwargs))
        if self.create_exc is not None:
            raise self.create_exc
        return types.SimpleNamespace(
            id=f"s-{self.name}-{len(self.creates)}", host=None,
            status="queued" if kwargs.get("queue_only") else "idle")

    async def maybe_start_queued(self) -> int:
        self.drains += 1
        if self.drain_exc is not None:
            raise self.drain_exc
        return 0


class _Router:
    """A router whose capacity verdict is scripted.

    ``with_room`` is what ``pick`` returns normally; ``healthy`` is what it returns
    when the capacity leg is ignored (hosts that are up but full); ``reachable`` is
    the fan-out set. ``engine_refusal`` makes every pick raise the engine subclass.
    """

    def __init__(self, orchs: dict[str, _Orch], *, with_room: list[str],
                 healthy: list[str], reachable: list[str] | None = None,
                 engine_refusal: bool = False) -> None:
        self._orchs = orchs
        self.with_room = list(with_room)
        self.healthy = list(healthy)
        self.reachable = list(reachable if reachable is not None else orchs)
        self.engine_refusal = engine_refusal
        self.picks: list[dict] = []

    def get(self, name):
        return self._orchs[name]

    def names(self):
        return sorted(self._orchs)

    async def reachable_names(self, *, always=None):
        return sorted(set(self.reachable) | ({always} if always else set()))

    async def pick(self, *, prefer=None, engine=None, expected_contract_sha256=None,
                   ignore_capacity=False):
        self.picks.append(dict(prefer=prefer, engine=engine, ignore_capacity=ignore_capacity))
        if self.engine_refusal:
            raise NoEngineHostAvailable(engine or AgentEngine.CODEX, {"dobby": "binary_not_found"})
        order = self.healthy if ignore_capacity else self.with_room
        if not order:
            raise NoHostAvailable("no enabled, healthy host with capacity; refusing to run "
                                  "in-process to avoid a split-brain over the shared database")
        return list(order)


def _fleet(router: _Router, primary: str = "dobby") -> FleetOrchestrator:
    fo = FleetOrchestrator(cast(FleetRouter, router), primary=primary)
    fo.stamped = []  # type: ignore[attr-defined]

    async def _stamp(session_id: str, host: str) -> None:
        fo.stamped.append((session_id, host))  # type: ignore[attr-defined]

    fo._stamp_host = _stamp  # type: ignore[method-assign]
    return fo


# --- create: a full fleet queues instead of refusing -------------------------

@pytest.mark.asyncio
async def test_all_hosts_full_queues_on_the_primary():
    """dobby 5/4, rocky 8/6, both healthy: the session is persisted as queued on
    the primary rather than refused, and stamped for it like any dispatch."""
    dobby, rocky = _Orch("dobby"), _Orch("rocky")
    router = _Router({"dobby": dobby, "rocky": rocky}, with_room=[], healthy=["rocky", "dobby"])
    fo = _fleet(router)

    rec = await fo.create_session(name="x", initial_prompt="hi")

    assert rec.status == "queued"
    assert dobby.creates == [{"name": "x", "initial_prompt": "hi", "queue_only": True}]
    assert rocky.creates == []
    assert fo.stamped == [(rec.id, "dobby")]  # type: ignore[attr-defined]
    assert rec.host == "dobby"


@pytest.mark.asyncio
async def test_a_codex_launch_queues_with_its_engine():
    """The engine rides the create kwargs AND filters the queue hosts; both must
    reach the daemon without colliding."""
    dobby = _Orch("dobby")
    router = _Router({"dobby": dobby}, with_room=[], healthy=["dobby"])
    rec = await _fleet(router).create_session(name="x", engine="codex")
    assert rec.status == "queued"
    assert dobby.creates == [{"name": "x", "engine": AgentEngine.CODEX, "queue_only": True}]
    assert router.picks[-1] == dict(prefer=None, engine=AgentEngine.CODEX, ignore_capacity=True)


@pytest.mark.asyncio
async def test_a_fleet_with_room_never_sends_queue_only():
    dobby = _Orch("dobby")
    router = _Router({"dobby": dobby}, with_room=["dobby"], healthy=["dobby"])
    rec = await _fleet(router).create_session(name="x")
    assert rec.status == "idle"
    assert "queue_only" not in dobby.creates[0]


@pytest.mark.asyncio
async def test_queue_falls_over_to_the_next_reachable_host_when_the_primary_is_down():
    """The primary black-holes the create (never reached it): the queued row lands
    on another healthy host rather than surfacing as an error."""
    dobby = _Orch("dobby", create_exc=DaemonUnavailable("connect failed"))
    rocky = _Orch("rocky")
    router = _Router({"dobby": dobby, "rocky": rocky}, with_room=[], healthy=["rocky"])
    fo = _fleet(router)

    rec = await fo.create_session(name="x")

    assert rec.status == "queued"
    assert rocky.creates == [{"name": "x", "queue_only": True}]
    assert fo.stamped == [(rec.id, "rocky")]  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_queue_prefers_a_reachable_primary_even_when_it_is_unhealthy():
    """No host is healthy (say both are skew-fenced) but the primary answers:
    persisting the queued row there beats an error — the row is durable and
    starts once a host is dispatchable again."""
    dobby, rocky = _Orch("dobby"), _Orch("rocky")
    router = _Router({"dobby": dobby, "rocky": rocky}, with_room=[], healthy=[],
                     reachable=["dobby", "rocky"])
    rec = await _fleet(router).create_session(name="x")
    assert rec.status == "queued"
    assert dobby.creates and dobby.creates[0]["queue_only"] is True
    assert rocky.creates == []


@pytest.mark.asyncio
async def test_no_reachable_host_at_all_is_still_the_typed_refusal():
    """Nothing can persist the row: the original NoHostAvailable (not a transport
    error) reaches the API so the 409 envelope is unchanged."""
    dobby = _Orch("dobby", create_exc=DaemonUnavailable("down"))
    router = _Router({"dobby": dobby}, with_room=[], healthy=[], reachable=[])
    with pytest.raises(NoHostAvailable) as ei:
        await _fleet(router).create_session(name="x")
    assert not isinstance(ei.value, NoEngineHostAvailable)
    # It tried the primary anyway (`always` keeps it in the reachable set) and only
    # then gave up.
    assert dobby.creates == [{"name": "x", "queue_only": True}]


@pytest.mark.asyncio
async def test_an_old_daemon_that_rejects_queue_only_does_not_mask_the_refusal():
    from server.ipc.client import DaemonError
    dobby = _Orch("dobby", create_exc=DaemonError(
        "TypeError", "create_session() got an unexpected keyword argument 'queue_only'"))
    router = _Router({"dobby": dobby}, with_room=[], healthy=["dobby"])
    with pytest.raises(NoHostAvailable) as ei:
        await _fleet(router).create_session(name="x")
    assert "capacity" in str(ei.value)


@pytest.mark.asyncio
async def test_an_unknown_outcome_while_queueing_is_not_retried_elsewhere():
    """Same rule as a normal dispatch: the daemon may have persisted the row
    after the edge gave up, so cascading would queue it twice."""
    dobby = _Orch("dobby", create_exc=DaemonUnavailable("timed out", outcome_unknown=True))
    rocky = _Orch("rocky")
    router = _Router({"dobby": dobby, "rocky": rocky}, with_room=[], healthy=["dobby", "rocky"])
    with pytest.raises(DaemonUnavailable):
        await _fleet(router).create_session(name="x")
    assert rocky.creates == []


@pytest.mark.asyncio
async def test_an_engine_refusal_is_never_turned_into_a_queue():
    """"Install the Codex binary" must stay the answer — a queued session would
    wait forever for a runner that does not exist."""
    dobby = _Orch("dobby")
    router = _Router({"dobby": dobby}, with_room=[], healthy=["dobby"], engine_refusal=True)
    with pytest.raises(NoEngineHostAvailable):
        await _fleet(router).create_session(name="x", engine="codex")
    assert dobby.creates == []


@pytest.mark.asyncio
async def test_the_assistant_is_never_queued():
    """The drawer's session ignores capacity already (#2184); a refusal there is a
    health problem, and an assistant stuck in a queue would be a closed drawer."""
    from server.sessions.types import ASSISTANT_KIND
    dobby = _Orch("dobby")
    router = _Router({"dobby": dobby}, with_room=[], healthy=[])
    with pytest.raises(NoHostAvailable):
        await _fleet(router).create_session(name="x", kind=ASSISTANT_KIND)
    assert dobby.creates == []


# --- the edge nudges hosts with headroom to drain ------------------------------

@pytest.mark.asyncio
async def test_drain_nudge_asks_only_hosts_with_headroom():
    dobby, rocky = _Orch("dobby"), _Orch("rocky")
    router = _Router({"dobby": dobby, "rocky": rocky}, with_room=["rocky"], healthy=["dobby", "rocky"])
    fo = _fleet(router)

    async def _queued() -> int:
        return 2

    fo._queued_count = _queued  # type: ignore[method-assign]
    assert await fo.drain_queue_once() == ["rocky"]
    assert (dobby.drains, rocky.drains) == (0, 1)


@pytest.mark.asyncio
async def test_drain_nudge_is_silent_when_nothing_is_queued():
    dobby = _Orch("dobby")
    router = _Router({"dobby": dobby}, with_room=["dobby"], healthy=["dobby"])
    fo = _fleet(router)

    async def _queued() -> int:
        return 0

    fo._queued_count = _queued  # type: ignore[method-assign]
    assert await fo.drain_queue_once() == []
    assert dobby.drains == 0
    assert router.picks == []


@pytest.mark.asyncio
async def test_drain_nudge_survives_a_full_fleet_and_a_dead_host():
    """No host with room → nothing to ask (not an error); a host that fails the
    RPC is skipped and the others still get their nudge."""
    dobby = _Orch("dobby", drain_exc=DaemonUnavailable("down"))
    rocky = _Orch("rocky")
    full = _Router({"dobby": dobby, "rocky": rocky}, with_room=[], healthy=["dobby", "rocky"])
    fo = _fleet(full)

    async def _queued() -> int:
        return 1

    fo._queued_count = _queued  # type: ignore[method-assign]
    assert await fo.drain_queue_once() == []

    both = _Router({"dobby": dobby, "rocky": rocky}, with_room=["dobby", "rocky"],
                   healthy=["dobby", "rocky"])
    fo = _fleet(both)
    fo._queued_count = _queued  # type: ignore[method-assign]
    assert await fo.drain_queue_once() == ["rocky"]
    assert (dobby.drains, rocky.drains) == (1, 1)
