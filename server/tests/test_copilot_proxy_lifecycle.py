import asyncio
import os
import types
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


# --- orphan process sweep (startup self-healing) ---

class _ProcInfo:
    """Minimal /proc stand-in for sweep_orphans (mirrors adopt.ProcInfo)."""
    def __init__(self, pid, cmdline):
        self.pid = pid
        self.cmdline = cmdline


def test_sweep_orphans_reaps_stray_and_spares_live():
    """An orphaned copilot2api on a different port is reaped; one on a live port is spared."""
    async def go():
        mgr, procs = _mgr()
        await mgr.ensure("c1")  # takes port 4142
        live_port = mgr.port_for("c1")
        assert live_port == 4142

        killed_pids = []
        fake_procs = [
            _ProcInfo(999, ["copilot2api", "-port", "4143", "-token-dir", "/tmp/tok/other"]),  # stray port
            _ProcInfo(888, ["copilot2api", "-port", str(live_port), "-token-dir", "/tmp/tok/c1"]),  # live port
        ]

        n = mgr.sweep_orphans(
            lister=lambda: fake_procs,
            killer=lambda pid: killed_pids.append(pid) or True
        )

        assert n == 1
        assert killed_pids == [999]
    asyncio.run(go())


def test_sweep_orphans_ignores_non_copilot_processes():
    """Non-copilot2api processes are never reaped."""
    async def go():
        mgr, procs = _mgr()
        await mgr.ensure("c1")
        killed_pids = []
        fake_procs = [
            _ProcInfo(777, ["python", "-m", "server.app"]),
            _ProcInfo(666, ["copilot2api", "-port", "4999"]),  # copilot but not live
        ]

        n = mgr.sweep_orphans(
            lister=lambda: fake_procs,
            killer=lambda pid: killed_pids.append(pid) or True
        )

        # Only the copilot2api on a non-live port is killed
        assert n == 1
        assert killed_pids == [666]
    asyncio.run(go())


def test_sweep_orphans_counts_only_signalled():
    """A killer returning False (pid already gone) is not counted."""
    async def go():
        mgr, procs = _mgr()
        await mgr.ensure("c1")
        fake_procs = [
            _ProcInfo(111, ["copilot2api", "-port", "4143"]),
            _ProcInfo(112, ["copilot2api", "-port", "4144"]),
        ]

        n = mgr.sweep_orphans(
            lister=lambda: fake_procs,
            killer=lambda pid: pid == 111  # only 111 succeeds
        )

        assert n == 1
    asyncio.run(go())


def test_build_returns_none_when_disabled():
    """build_copilot_proxy_manager returns None when enable_copilot is False."""
    settings = types.SimpleNamespace(
        enable_copilot=False,
        copilot_port_base=4142,
        copilot_port_count=8,
        copilot_idle_grace_s=300.0,
    )
    mgr = cp.build_copilot_proxy_manager(settings)
    assert mgr is None


def test_build_returns_none_when_binary_absent(monkeypatch):
    """build_copilot_proxy_manager returns None when binary not found."""
    monkeypatch.setattr(cp, "resolve_copilot_proxy", lambda: None)
    settings = types.SimpleNamespace(
        enable_copilot=True,
        copilot_port_base=4142,
        copilot_port_count=8,
        copilot_idle_grace_s=300.0,
    )
    mgr = cp.build_copilot_proxy_manager(settings)
    assert mgr is None


def test_build_returns_manager_when_enabled():
    """build_copilot_proxy_manager returns a CopilotProxyManager instance when enabled and binary present."""
    settings = types.SimpleNamespace(
        enable_copilot=True,
        copilot_port_base=4142,
        copilot_port_count=8,
        copilot_idle_grace_s=300.0,
    )
    mgr = cp.build_copilot_proxy_manager(settings)
    assert isinstance(mgr, cp.CopilotProxyManager)
    assert mgr is not None
