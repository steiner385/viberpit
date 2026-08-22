# server/tests/test_daemon_shutdown_x11.py
"""Issue #206 (daemon half): the session daemon must tear down virtual displays on
shutdown. orchestrator.stop() only cancels background tasks, so without an explicit
sweep the daemon-owned Xvfb/x11vnc/chrome leak on every restart."""
import types

import pytest

from server.ipc.serve import daemon_lifespan


class _X11:
    def __init__(self):
        self.stop_all_calls = 0
        self.sweep_stale_calls = 0
        self.sweep_orphan_calls = 0
    async def stop_all(self, *a, **k): self.stop_all_calls += 1
    def sweep_stale(self): self.sweep_stale_calls += 1; return 0
    def sweep_orphan_processes(self): self.sweep_orphan_calls += 1; return 0


class _Orch:
    def __init__(self, x11):
        self._x11 = x11
        self.stopped = False
    async def rehydrate(self): return 0
    async def rearm_background_tasks(self): return 0
    async def start(self): pass
    async def stop(self): self.stopped = True


class _App:  # minimal stand-in for the daemon's FastAPI/Starlette app; real apps always
             # have a `.state` namespace, so this fixture carries one too.
    def __init__(self):
        self.state = types.SimpleNamespace()


@pytest.mark.asyncio
async def test_daemon_shutdown_tears_down_displays(monkeypatch):
    monkeypatch.setenv("CCSWITCH_DB_URL", "sqlite+aiosqlite:///:memory:")
    x11 = _X11()
    orch = _Orch(x11)
    cm = daemon_lifespan(_App(), orch)
    await cm.__aenter__()
    await cm.__aexit__(None, None, None)
    assert x11.stop_all_calls == 1   # displays swept before stop
    assert orch.stopped is True


@pytest.mark.asyncio
async def test_daemon_startup_reaps_orphan_displays(monkeypatch):
    """Issue #206 (startup half): the daemon swept nothing at boot, so orphaned
    Xvfb/x11vnc/chrome from a prior SIGKILLed run accumulated. Startup must now both
    unlink stale files and reap orphan processes."""
    monkeypatch.setenv("CCSWITCH_DB_URL", "sqlite+aiosqlite:///:memory:")
    x11 = _X11()
    orch = _Orch(x11)
    cm = daemon_lifespan(_App(), orch)
    await cm.__aenter__()
    try:
        assert x11.sweep_orphan_calls == 1
        assert x11.sweep_stale_calls == 1
    finally:
        await cm.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_daemon_shutdown_ok_without_x11(monkeypatch):
    # A daemon with x11 disabled (no manager) must still shut down cleanly.
    monkeypatch.setenv("CCSWITCH_DB_URL", "sqlite+aiosqlite:///:memory:")
    orch = _Orch(None)
    cm = daemon_lifespan(_App(), orch)
    await cm.__aenter__()
    await cm.__aexit__(None, None, None)
    assert orch.stopped is True
