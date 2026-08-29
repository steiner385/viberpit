import pytest
from server.sessions.fleet_orchestrator import FleetOrchestrator
from server.sessions.facade import _DEADLINE_ATTR


class _FakeOrch:
    def __init__(self, name): self.name = name; self.calls = []
    async def list_sessions(self): self.calls.append("list_sessions"); return [self.name]
    async def usage(self, acct, *, timeout=None):  # mirrors a @rpc_deadline method
        self.calls.append(("usage", acct, timeout)); return {"acct": acct}
    usage.__ccswitch_rpc_deadline__ = True
    def daemon_sha(self): return "abc1234"


class _FakeRouter:
    def __init__(self, orchs): self._orchs = orchs
    def get(self, name): return self._orchs[name]
    def names(self): return sorted(self._orchs)
    async def pick(self, *, prefer=None): return sorted(self._orchs)


@pytest.mark.asyncio
async def test_unrouted_method_goes_to_primary():
    prim = _FakeOrch("rocky")
    fo = FleetOrchestrator(_FakeRouter({"rocky": prim}), primary="rocky")
    assert await fo.list_sessions() == ["rocky"]
    assert prim.calls == ["list_sessions"]


@pytest.mark.asyncio
async def test_primary_method_preserves_rpc_deadline_marker():
    prim = _FakeOrch("rocky")
    fo = FleetOrchestrator(_FakeRouter({"rocky": prim}), primary="rocky")
    # The exposed callable must still be recognised as native-deadline-capable,
    # or call_with_deadline cancels the socket (#734).
    assert getattr(fo.usage, _DEADLINE_ATTR, False) is True


def test_sync_cached_read_passes_through():
    prim = _FakeOrch("rocky")
    fo = FleetOrchestrator(_FakeRouter({"rocky": prim}), primary="rocky")
    assert fo.daemon_sha() == "abc1234"


@pytest.mark.asyncio
async def test_per_session_op_routes_to_owning_host(monkeypatch):
    rocky, dobby = _FakeOrch("rocky"), _FakeOrch("dobby")

    async def get_session(self, sid):  # per-session method on the fakes
        self.calls.append(("get_session", sid)); return {"id": sid, "host": self.name}
    _FakeOrch.get_session = get_session

    fo = FleetOrchestrator(_FakeRouter({"rocky": rocky, "dobby": dobby}), primary="rocky")

    async def fake_host_for(sid):  # stand in for the DB lookup
        return {"s-on-dobby": "dobby", "s-on-rocky": "rocky"}.get(sid)
    monkeypatch.setattr(fo, "_host_for", fake_host_for)

    await fo.get_session("s-on-dobby")
    await fo.get_session("s-on-rocky")
    assert ("get_session", "s-on-dobby") in dobby.calls
    assert ("get_session", "s-on-rocky") in rocky.calls


@pytest.mark.asyncio
async def test_per_session_marked_method_keeps_deadline_marker():
    fo = FleetOrchestrator(_FakeRouter({"rocky": _FakeOrch("rocky")}), primary="rocky")
    assert getattr(fo.ensure_driver, _DEADLINE_ATTR, False) is True


@pytest.mark.asyncio
async def test_unknown_host_falls_back_to_primary(monkeypatch):
    rocky = _FakeOrch("rocky")

    async def get_session(self, sid): self.calls.append(("get_session", sid)); return {}
    _FakeOrch.get_session = get_session
    fo = FleetOrchestrator(_FakeRouter({"rocky": rocky}), primary="rocky")
    monkeypatch.setattr(fo, "_host_for", lambda sid: _acoro(None))
    await fo.get_session("orphan")
    assert ("get_session", "orphan") in rocky.calls


async def _acoro(v): return v
