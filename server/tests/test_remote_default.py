# server/tests/test_remote_default.py
import os
from unittest import mock
import pytest
from server.app import create_app, _daemon_compatible
from server.sessions.facade import InProcessOrchestratorAdapter
from server.sessions.remote import RemoteOrchestrator
from server.ipc.daemon import create_daemon_app
from server.tests.ipc_harness import serve_daemon_uds


def _clean_env():
    e = {k: v for k, v in os.environ.items() if k != "CCSWITCH_SESSIOND_SOCKET"}
    return e


def test_default_in_process_when_no_daemon():
    # No daemon at the well-known path → in-process fallback (byte-identical to today).
    with mock.patch.dict(os.environ, _clean_env(), clear=True), \
         mock.patch("server.app._daemon_compatible", return_value=False):
        app = create_app()
    assert app.state.remote_mode is False
    assert isinstance(app.state.sessions, InProcessOrchestratorAdapter)


def test_default_remote_when_compatible_daemon_present():
    with mock.patch.dict(os.environ, _clean_env(), clear=True), \
         mock.patch("server.app._daemon_compatible", return_value=True):
        app = create_app()
    assert app.state.remote_mode is True
    assert isinstance(app.state.sessions, RemoteOrchestrator)
    assert app.state.orchestrator is None


def test_explicit_env_forces_remote_without_probe():
    # Explicit flag → remote unconditionally, even with NO daemon (probe not consulted).
    with mock.patch.dict(os.environ, {"CCSWITCH_SESSIOND_SOCKET": "/tmp/dead.sock"}), \
         mock.patch("server.app._daemon_compatible", return_value=False) as probe:
        app = create_app()
    assert app.state.remote_mode is True
    probe.assert_not_called()


def test_injected_orchestrator_forces_in_process():
    from server.sessions.orchestrator import SessionOrchestrator
    orch = SessionOrchestrator()
    with mock.patch("server.app._daemon_compatible", return_value=True):
        app = create_app(orchestrator=orch)
    assert app.state.remote_mode is False


@pytest.mark.asyncio
async def test_daemon_compatible_probe_real_socket():
    import asyncio
    loop = asyncio.get_running_loop()
    app = create_daemon_app()
    async with serve_daemon_uds(app) as sock:
        # _daemon_compatible is sync (create_app is sync); run in a thread so
        # the event loop stays free to serve the in-process uvicorn daemon.
        result = await loop.run_in_executor(None, _daemon_compatible, sock)
        assert result is True
    # After the daemon stops, the socket is gone → not compatible.
    assert _daemon_compatible("/nonexistent/ccsd.sock") is False
