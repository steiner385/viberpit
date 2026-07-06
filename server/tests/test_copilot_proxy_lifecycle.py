import asyncio
import pytest
from server.sessions import copilot_proxy as cp
from server.tests.test_copilot_proxy_ensure import FakeProc, _mgr


@pytest.fixture(autouse=True)
def _patch_resolve(monkeypatch):
    monkeypatch.setattr(cp, "resolve_copilot_proxy", lambda: "/usr/bin/copilot2api")


def test_idle_detach_tears_down_after_grace():
    async def go():
        mgr, procs = _mgr(idle_grace_s=0.05)
        await mgr.ensure("c1"); mgr.attach("c1")
        mgr.detach("c1")
        await asyncio.sleep(0.15)
        assert mgr.port_for("c1") is None
        assert procs[0][1].terminated
    asyncio.run(go())


def test_stop_all_terminates_every_proxy():
    async def go():
        mgr, procs = _mgr()
        await mgr.ensure("c1"); await mgr.ensure("c2")
        await mgr.stop_all()
        assert mgr.port_for("c1") is None and mgr.port_for("c2") is None
    asyncio.run(go())


def test_usage_uses_injected_fetch():
    async def go():
        mgr, procs = _mgr(usage_fetch=lambda port: {"premium": {"used": 1, "limit": 10}})
        await mgr.ensure("c1")
        assert mgr.usage("c1") == {"premium": {"used": 1, "limit": 10}}
        assert mgr.usage("absent") is None
    asyncio.run(go())
