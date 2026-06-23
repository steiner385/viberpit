# server/tests/test_ipc_handshake.py
import os
from unittest import mock
import pytest
from starlette.testclient import TestClient
from server.ipc.daemon import create_daemon_app
from server.ipc import protocol as proto
from server.ipc.paths import DEFAULT_SOCKET_PATH, sessiond_socket
from server.ipc.client import DaemonClient, DaemonUnavailable, protocol_compatible
from server.tests.ipc_harness import serve_daemon_uds


def test_default_socket_path_and_env_override():
    assert DEFAULT_SOCKET_PATH == "/run/ccswitch/sessiond.sock"
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("CCSWITCH_SESSIOND_SOCKET", None)
        assert sessiond_socket() == DEFAULT_SOCKET_PATH
    with mock.patch.dict(os.environ, {"CCSWITCH_SESSIOND_SOCKET": "/tmp/x.sock"}):
        assert sessiond_socket() == "/tmp/x.sock"


def test_daemon_info_and_healthz_routes():
    app = create_daemon_app()
    client = TestClient(app)
    info = client.get("/info")
    assert info.status_code == 200
    assert info.json()["protocol"] == proto.PROTOCOL_VERSION
    assert "sha" in info.json()
    hz = client.get("/healthz")
    assert hz.status_code == 200
    assert hz.json()["status"] == "ok"


def test_protocol_compatible():
    assert protocol_compatible(proto.PROTOCOL_VERSION) is True
    assert protocol_compatible(proto.PROTOCOL_VERSION + 1) is False


@pytest.mark.asyncio
async def test_client_info_over_uds_and_dead_socket():
    app = create_daemon_app()
    async with serve_daemon_uds(app) as sock:
        client = DaemonClient(sock)
        try:
            info = await client.info()
            assert info["protocol"] == proto.PROTOCOL_VERSION
        finally:
            await client.aclose()
    dead = DaemonClient("/nonexistent/ccsd.sock")
    with pytest.raises(DaemonUnavailable):
        await dead.info()
    await dead.aclose()
