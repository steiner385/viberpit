# server/tests/test_ipc_serve.py
from unittest import mock
import pytest
from server.ipc.serve import build_daemon, daemon_lifespan

_FAKE_X11 = object()  # sentinel — build_x11_manager returns None in X11-less CI


def test_build_daemon_wires_orchestrator_and_rpc():
    orch = mock.MagicMock()
    with mock.patch("server.ipc.serve.build_x11_manager", return_value=_FAKE_X11):
        app, returned = build_daemon(orchestrator=orch)
    assert returned is orch
    # x11 manager shared onto the orchestrator (mirrors the edge).
    assert orch._x11 is not None
    # RPC surface registered (a representative method).
    assert "ensure_driver" in app.state.rpc
    assert "list_sessions" in app.state.rpc


@pytest.mark.asyncio
async def test_daemon_lifespan_starts_and_stops_orchestrator():
    orch = mock.MagicMock()
    orch.rehydrate = mock.AsyncMock()
    orch.start = mock.AsyncMock()
    orch.stop = mock.AsyncMock()
    with mock.patch("server.ipc.serve.build_x11_manager", return_value=_FAKE_X11):
        app, _ = build_daemon(orchestrator=orch)
    with mock.patch("server.ipc.serve.init_db", new=mock.AsyncMock()) as initdb:
        async with daemon_lifespan(app, orch):
            initdb.assert_awaited_once()
            orch.start.assert_awaited_once()
        orch.stop.assert_awaited_once()
