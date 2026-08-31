# server/tests/test_ipc_serve.py
from unittest import mock
import pytest
from server.ipc.serve import build_daemon, daemon_lifespan
from server.ipc.client import DaemonClient, protocol_compatible
from server.tests.ipc_harness import serve_daemon_uds
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.types import SessionRecord

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


@pytest.mark.asyncio
async def test_built_daemon_serves_handshake_and_rpc_over_uds():
    orch = SessionOrchestrator(sdk_factory=mock.MagicMock(), poll_interval=9999.0,
                               usage_fn=lambda n: None)
    orch._records["s1"] = SessionRecord(id="s1", name="S1", account="acct",
                                        claude_session_id="cs", cols=80, rows=24, cwd="/tmp")
    app, _ = build_daemon(orchestrator=orch)
    async with serve_daemon_uds(app) as sock:
        client = DaemonClient(sock)
        try:
            info = await client.info()
            assert protocol_compatible(info["protocol"]) is True
            listed = await client.call("list_sessions")
            assert [r["id"] for r in listed["sessions"]] == ["s1"]
        finally:
            await client.aclose()


@pytest.mark.asyncio
async def test_daemon_lifespan_installs_drain_on_sigterm_and_uninstalls_on_exit():
    """The daemon must protect in-flight turns on SIGTERM itself (see
    test_daemon_drain_on_sigterm.py) — wired for the lifetime of the lifespan."""
    orch = mock.MagicMock()
    orch.rehydrate = mock.AsyncMock()
    orch.start = mock.AsyncMock()
    orch.stop = mock.AsyncMock()
    with mock.patch("server.ipc.serve.build_x11_manager", return_value=_FAKE_X11):
        app, _ = build_daemon(orchestrator=orch)
    uninstall = mock.MagicMock()
    with mock.patch("server.ipc.serve.init_db", new=mock.AsyncMock()), \
         mock.patch("server.ipc.serve.install_drain_on_sigterm", return_value=uninstall) as install:
        async with daemon_lifespan(app, orch):
            install.assert_called_once_with(orch)
            uninstall.assert_not_called()
        uninstall.assert_called_once()
