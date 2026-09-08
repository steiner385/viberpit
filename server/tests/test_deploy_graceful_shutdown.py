# server/tests/test_deploy_graceful_shutdown.py
"""Regression coverage for the transcript-audit V4 defect: uvicorn's graceful
shutdown waited indefinitely for open connections (attached browser WebSockets
never close on their own), so SIGTERM hung and systemd SIGKILLed the process at
TimeoutStopSec on every deploy before the lifespan shutdown branch (session
close / x11 sweep / DB dispose) ran. Both the edge (CLI-invoked uvicorn) and the
daemon (uvicorn.run() called from Python) must bound the graceful window."""
from pathlib import Path
from unittest import mock

from server.ipc import serve as ipc_serve

ROOT = Path(__file__).resolve().parents[2]


def test_edge_unit_bounds_graceful_shutdown():
    unit = (ROOT / "deploy" / "viberpit.service").read_text()
    assert "ExecStart=" in unit and "server.app:app" in unit
    exec_start_line = next(
        line for line in unit.splitlines() if line.startswith("ExecStart=")
    )
    assert "--timeout-graceful-shutdown" in exec_start_line


def test_daemon_serve_passes_graceful_shutdown_timeout():
    """serve() can't set a CLI flag (it calls uvicorn.run() directly), so assert
    the kwarg is actually threaded through instead."""
    with mock.patch.object(ipc_serve, "build_daemon", return_value=(mock.MagicMock(), mock.MagicMock())), \
         mock.patch.object(ipc_serve, "uvicorn") as fake_uvicorn:
        ipc_serve.serve(socket_path="/tmp/does-not-matter.sock")
    assert fake_uvicorn.run.call_count == 1
    _, kwargs = fake_uvicorn.run.call_args
    assert kwargs["timeout_graceful_shutdown"] == ipc_serve._DAEMON_GRACEFUL_SHUTDOWN_TIMEOUT_S
