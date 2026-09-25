# server/tests/test_keepalive_outlives_client_pools.py
"""Every in-fleet httpx-pool -> uvicorn hop must have the server's keep-alive OUTLIVE
the client pool's expiry (server/http_keepalive.py).

#1614: both defaulted to 5 s; equal timers race and the edge saw "Server disconnected
without sending a response" from a healthy rocky daemon several times a day, which
Error Triage filed as an outage. The same tie existed on the edge -> deploy-controller
hop, which the UI polls on the same 5 s cadence, and the assistant loopback client ->
edge uvicorn hop flagged by #1632. Three hops, one invariant, pinned here on both
sides of each so neither can drift back into a tie.
"""
from pathlib import Path
from unittest import mock

import httpx

from server import http_keepalive
from server.assistant import client as assistant_client
from server.deploy import client as deploy_client
from server.deploy import serve as deploy_serve
from server.ipc import client as ipc_client
from server.ipc import serve as ipc_serve

ROOT = Path(__file__).resolve().parents[2]


def test_server_keepalive_is_well_above_client_pool_expiry():
    assert http_keepalive.SERVER_KEEPALIVE_TIMEOUT_S >= 2 * http_keepalive.CLIENT_KEEPALIVE_EXPIRY_S


def test_both_client_pools_use_the_shared_expiry():
    assert ipc_client._LIMITS.keepalive_expiry == http_keepalive.CLIENT_KEEPALIVE_EXPIRY_S
    assert deploy_client._LIMITS.keepalive_expiry == http_keepalive.CLIENT_KEEPALIVE_EXPIRY_S


def test_deploy_client_actually_passes_its_limits(tmp_path):
    """A `_LIMITS` nobody passes is not a fix: read the expiry back off the live pool."""
    c = deploy_client.DeployControllerClient(str(tmp_path / "x.sock"))
    pool = c._http._transport._pool
    assert pool._keepalive_expiry == http_keepalive.CLIENT_KEEPALIVE_EXPIRY_S
    assert isinstance(c._http, httpx.AsyncClient)


def test_daemon_serve_passes_keepalive_timeout_to_uvicorn():
    with mock.patch.object(ipc_serve, "build_daemon",
                           return_value=(mock.MagicMock(), mock.MagicMock())), \
         mock.patch.object(ipc_serve, "uvicorn") as fake_uvicorn:
        ipc_serve.serve(socket_path="/tmp/does-not-matter.sock")
    assert fake_uvicorn.run.call_count == 1
    _, kwargs = fake_uvicorn.run.call_args
    assert kwargs["timeout_keep_alive"] == http_keepalive.SERVER_KEEPALIVE_TIMEOUT_S


def test_controller_serve_passes_keepalive_timeout_to_uvicorn(tmp_path):
    with mock.patch.object(deploy_serve, "build_controller_app",
                           return_value=(mock.MagicMock(), None, None)), \
         mock.patch("server.github.app_auth.register_git_providers"), \
         mock.patch.object(deploy_serve, "uvicorn") as fake_uvicorn:
        deploy_serve.serve(socket_path=str(tmp_path / "deploy.sock"))
    assert fake_uvicorn.run.call_count == 1
    _, kwargs = fake_uvicorn.run.call_args
    assert kwargs["timeout_keep_alive"] == http_keepalive.SERVER_KEEPALIVE_TIMEOUT_S


def test_assistant_client_pool_uses_the_shared_expiry():
    assert assistant_client._LIMITS.keepalive_expiry == http_keepalive.CLIENT_KEEPALIVE_EXPIRY_S


def test_assistant_client_actually_passes_its_limits():
    """A `_LIMITS` nobody passes is not a fix: read the expiry back off the live pool
    (no transport override, so AssistantApi builds its own from `limits=`)."""
    api = assistant_client.AssistantApi(base_url="http://127.0.0.1:1", token="ccas1:x",
                                         session_id="s")
    pool = api._client._transport._pool
    assert pool._keepalive_expiry == http_keepalive.CLIENT_KEEPALIVE_EXPIRY_S


def test_edge_unit_keepalive_outlives_assistant_client():
    """deploy/viberpit.service is CLI-launched uvicorn, unlike the daemon/controller
    which call uvicorn.run() programmatically — the invariant has to be pinned on the
    unit file's ExecStart= flag instead of a kwarg."""
    unit = (ROOT / "deploy" / "viberpit.service").read_text()
    exec_start_line = next(
        line for line in unit.splitlines() if line.startswith("ExecStart=")
    )
    assert "--timeout-keep-alive" in exec_start_line
    flag_value = int(exec_start_line.split("--timeout-keep-alive")[1].split()[0])
    assert flag_value == http_keepalive.SERVER_KEEPALIVE_TIMEOUT_S
    assert flag_value >= 2 * http_keepalive.CLIENT_KEEPALIVE_EXPIRY_S
