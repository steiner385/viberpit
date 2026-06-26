import ssl
from unittest import mock

import httpx
import pytest

from server.ipc.paths import Endpoint
from server.ipc.serve import build_daemon, uvicorn_run_kwargs
from server.ipc.tls import DaemonTLSFiles, ClientTLSFiles, client_ssl_context
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.ipc_harness import serve_daemon_tls


def test_uvicorn_kwargs_unix():
    kw = uvicorn_run_kwargs(Endpoint("unix", path="/tmp/x.sock"))
    assert kw == {"uds": "/tmp/x.sock"}


def test_uvicorn_kwargs_https_enforces_client_certs(mtls_certs):
    tls = DaemonTLSFiles(cert=mtls_certs.server_cert, key=mtls_certs.server_key, ca=mtls_certs.ca)
    kw = uvicorn_run_kwargs(Endpoint("https", host="127.0.0.1", port=9443), daemon_tls=tls)
    assert kw["host"] == "127.0.0.1" and kw["port"] == 9443
    assert kw["ssl_certfile"] == mtls_certs.server_cert
    assert kw["ssl_ca_certs"] == mtls_certs.ca
    assert kw["ssl_cert_reqs"] == ssl.CERT_REQUIRED  # this is what makes it mutual


@pytest.mark.asyncio
async def test_daemon_serves_info_over_mtls(mtls_certs):
    orch = SessionOrchestrator(sdk_factory=mock.MagicMock(), poll_interval=9999.0,
                               usage_fn=lambda n: None)
    app, _ = build_daemon(orchestrator=orch)
    async with serve_daemon_tls(app, mtls_certs) as ep:
        ctx = client_ssl_context(ClientTLSFiles(
            cert=mtls_certs.client_cert, key=mtls_certs.client_key, ca=mtls_certs.ca))
        async with httpx.AsyncClient(base_url=f"https://{ep.host}:{ep.port}",
                                     verify=ctx, timeout=5.0) as c:
            r = await c.get("/info")
            assert r.json()["protocol"] == 1


@pytest.mark.asyncio
async def test_daemon_rejects_client_without_cert(mtls_certs):
    orch = SessionOrchestrator(sdk_factory=mock.MagicMock(), poll_interval=9999.0,
                               usage_fn=lambda n: None)
    app, _ = build_daemon(orchestrator=orch)
    async with serve_daemon_tls(app, mtls_certs) as ep:
        # Trust the server CA but present NO client cert → handshake must fail.
        ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=mtls_certs.ca)
        with pytest.raises((ssl.SSLError, httpx.ConnectError, httpx.RemoteProtocolError,
                            httpx.ReadError, OSError)):
            async with httpx.AsyncClient(base_url=f"https://{ep.host}:{ep.port}",
                                         verify=ctx, timeout=5.0) as c:
                await c.get("/info")
