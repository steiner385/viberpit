"""Both processes that own sessions wire NativeDisplay the same way X11Manager is wired."""
import pytest
from fastapi import FastAPI

from server.ipc.x11_rpc import register_x11


class _Orch:
    def __init__(self, result): self.result = result; self.calls = []
    async def open_browser(self, sid, url): self.calls.append((sid, url)); return self.result


def _daemon_app() -> FastAPI:
    app = FastAPI()
    app.state.rpc = {}          # what server.ipc.daemon.register_rpc writes into
    return app


@pytest.mark.asyncio
async def test_daemon_open_browser_rpc_delegates_to_orchestrator():
    app = _daemon_app()
    orch = _Orch({"ok": False, "reason": "native_unavailable"})
    register_x11(app, None, orchestrator=orch)
    handler = app.state.rpc["x11_open_browser"]
    assert await handler({"session_id": "S", "url": "u"}) == {"ok": False, "reason": "native_unavailable"}
    assert orch.calls == [("S", "u")]


@pytest.mark.asyncio
async def test_daemon_open_browser_without_orchestrator_keeps_legacy_no_display():
    app = _daemon_app()
    register_x11(app, None)
    handler = app.state.rpc["x11_open_browser"]
    assert await handler({"session_id": "S"}) == {"ok": False, "reason": "no_display"}
