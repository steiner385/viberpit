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
