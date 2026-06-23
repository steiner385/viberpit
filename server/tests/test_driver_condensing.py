from __future__ import annotations
import pytest
from server.sessions.driver import SessionDriver
from server.sessions.permissions import PermissionRegistry
from server.sessions import events as ev
from server.tests.fakes.fake_sdk import FakeSDKClient


def _driver(**kw):
    def factory(cwd, config_dir, can_use_tool, mode, resume, *, on_pre_compact=None):
        return FakeSDKClient([], on_can_use_tool=can_use_tool)
    clock = {"t": 100.0}
    d = SessionDriver("sid", "alpha", "/tmp", "/tmp/cfg", mode="default",
                      sdk_factory=factory, registry=PermissionRegistry(),
                      identity="wstest@example.com", clock=lambda: clock["t"], **kw)
    return d, clock


@pytest.mark.asyncio
async def test_pre_compact_emits_estimate():
    async def est():
        return 8000.0
    d, _ = _driver(get_condense_estimate=est)
    await d._on_pre_compact()
    out = []
    while not d._out.empty():
        out.append(d._out.get_nowait())
    cond = [e for e in out if isinstance(e, ev.Condensing) and e.active]
    assert cond and cond[0].expected_ms == 8000.0


@pytest.mark.asyncio
async def test_boundary_records_clamped_elapsed():
    recorded = []
    async def rec(ms): recorded.append(ms)
    d, clock = _driver(on_record_condense_ms=rec)
    await d._on_pre_compact()           # stamps started_at at t=100
    clock["t"] = 105.0                  # 5s later
    # simulate the drain consuming the translator's boundary event:
    await d._consume_condensing(ev.Condensing(active=False))
    assert recorded == [5000.0]


@pytest.mark.asyncio
async def test_backstop_without_boundary_records_nothing():
    recorded = []
    async def rec(ms): recorded.append(ms)
    d, clock = _driver(on_record_condense_ms=rec)
    await d._on_pre_compact()
    clock["t"] = 110.0
    d._condensing = False
    d._condense_started_at = None       # backstop clears without recording
    assert recorded == []
