# server/tests/test_app_shutdown_teardown.py
"""Issue #206: the edge shutdown must tear down sessions concurrently/bounded and
guarantee a final display sweep so Xvfb/x11vnc/chrome are never leaked."""
import asyncio

import pytest
from fastapi import FastAPI

from server.app import _lifespan


class _Reauth:
    def sweep_stale(self): pass
    async def shutdown(self): pass


class _Orch:
    def __init__(self, drivers):
        self._drivers = dict.fromkeys(drivers)
        self.closed: list[str] = []
        self.stopped = False
    async def rehydrate(self): return 0
    async def start(self): pass
    async def stop(self): self.stopped = True
    async def close_session(self, sid): self.closed.append(sid)


class _X11:
    def __init__(self): self.shutdown_calls = 0; self.swept = 0
    def sweep_stale(self): self.swept += 1; return 0
    async def shutdown_all(self, timeout: float = 15.0): self.shutdown_calls += 1; return 0


def _make_app(orch, x11):
    app = FastAPI()
    app.state.remote_mode = False
    app.state.orchestrator = orch
    app.state.reauth = _Reauth()
    app.state.x11 = x11
    return app


@pytest.mark.asyncio
async def test_shutdown_closes_all_sessions_and_sweeps_displays(monkeypatch):
    monkeypatch.setenv("CCSWITCH_DEV", "1")
    monkeypatch.setenv("CCSWITCH_DB_URL", "sqlite+aiosqlite:///:memory:")
    orch = _Orch(["s1", "s2", "s3"])
    x11 = _X11()
    app = _make_app(orch, x11)
    async with _lifespan(app):
        pass  # exiting the context runs the shutdown branch
    # every live session was closed (order-independent — close runs concurrently)
    assert sorted(orch.closed) == ["s1", "s2", "s3"]
    # the guaranteed final display sweep fired exactly once
    assert x11.shutdown_calls == 1
    assert orch.stopped is True


@pytest.mark.asyncio
async def test_shutdown_sweeps_displays_even_when_a_close_hangs(monkeypatch):
    # A hung close must not stop the final display teardown from running — the
    # concurrent close is bounded, then shutdown_all sweeps regardless.
    monkeypatch.setenv("CCSWITCH_DEV", "1")
    monkeypatch.setenv("CCSWITCH_DB_URL", "sqlite+aiosqlite:///:memory:")
    x11 = _X11()

    class _HangOrch(_Orch):
        async def close_session(self, sid):
            self.closed.append(sid)
            await asyncio.sleep(3600)  # never returns

    orch = _HangOrch(["stuck"])
    app = _make_app(orch, x11)
    # Shrink the close budget so the test doesn't wait 20s.
    monkeypatch.setattr("server.app._SHUTDOWN_CLOSE_BUDGET_S", 0.1)
    async with _lifespan(app):
        pass
    assert x11.shutdown_calls == 1   # final sweep ran despite the hung close
    assert orch.stopped is True
