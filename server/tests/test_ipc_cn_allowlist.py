import os
from unittest import mock

from server.ipc.tls import peer_identities, client_allowlist, allowed

# getpeercert() shape: subject is a tuple of RDNs, each RDN a tuple of (key, value) pairs;
# subjectAltName is a tuple of (type, value) pairs.
_CERT = {
    "subject": ((("commonName", "edge"),),),
    "subjectAltName": (("DNS", "edge.internal"), ("IP Address", "10.0.0.5")),
}


def test_peer_identities_unions_cn_and_sans():
    assert peer_identities(_CERT) == {"edge", "edge.internal", "10.0.0.5"}


def test_peer_identities_cn_only():
    assert peer_identities({"subject": ((("commonName", "edge"),),)}) == {"edge"}


def test_peer_identities_empty_cert():
    assert peer_identities({}) == set()


def test_client_allowlist_unset_is_none():
    with mock.patch.dict(os.environ, {}, clear=True):
        assert client_allowlist() is None


def test_client_allowlist_blank_is_none():
    with mock.patch.dict(os.environ, {"CCSWITCH_SESSIOND_CLIENT_ALLOWLIST": "  ,, "}, clear=True):
        assert client_allowlist() is None


def test_client_allowlist_parses_trims_and_drops_empties():
    with mock.patch.dict(os.environ, {"CCSWITCH_SESSIOND_CLIENT_ALLOWLIST": " edge, edge.internal ,"}, clear=True):
        assert client_allowlist() == frozenset({"edge", "edge.internal"})


def test_allowed_cn_hit():
    assert allowed(_CERT, frozenset({"edge"})) is True


def test_allowed_san_hit():
    assert allowed(_CERT, frozenset({"edge.internal"})) is True


def test_allowed_miss():
    assert allowed(_CERT, frozenset({"someone-else"})) is False


import ssl
from unittest import mock as _mock

import httpx
import pytest

from server.ipc.paths import Endpoint
from server.ipc.serve import build_daemon, uvicorn_run_kwargs
from server.ipc.tls import ClientTLSFiles, client_ssl_context
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.ipc_harness import serve_daemon_tls


def _orch():
    return SessionOrchestrator(sdk_factory=_mock.MagicMock(), poll_interval=9999.0,
                               usage_fn=lambda n: None)


def _client_ctx(certs, cert, key):
    return client_ssl_context(ClientTLSFiles(cert=cert, key=key, ca=certs.ca))


def _serve(app, certs, allowlist):
    # serve_daemon_tls passes allowlist through to uvicorn_run_kwargs (see Step 4).
    # Plain def returning the async CM → call sites use `async with _serve(...)`.
    return serve_daemon_tls(app, certs, allowlist=allowlist)


def test_uvicorn_kwargs_adds_factory_only_when_allowlist_set(mtls_certs):
    from server.ipc.tls import DaemonTLSFiles
    tls = DaemonTLSFiles(cert=mtls_certs.server_cert, key=mtls_certs.server_key, ca=mtls_certs.ca)
    ep = Endpoint("https", host="127.0.0.1", port=9443)
    assert "ssl_context_factory" not in uvicorn_run_kwargs(ep, daemon_tls=tls, allowlist=None)
    kw = uvicorn_run_kwargs(ep, daemon_tls=tls, allowlist=frozenset({"edge"}))
    assert callable(kw["ssl_context_factory"])
    assert kw["ssl_cert_reqs"] == ssl.CERT_REQUIRED  # still mutual


@pytest.mark.asyncio
async def test_allowlisted_client_connects(mtls_certs):
    app, _ = build_daemon(orchestrator=_orch())
    async with _serve(app, mtls_certs, frozenset({"edge"})) as ep:
        ctx = _client_ctx(mtls_certs, mtls_certs.client_cert, mtls_certs.client_key)
        async with httpx.AsyncClient(base_url=f"https://{ep.host}:{ep.port}", verify=ctx, timeout=5.0) as c:
            assert (await c.get("/info")).json()["protocol"] == 1


@pytest.mark.asyncio
async def test_same_ca_client_not_in_allowlist_is_rejected(mtls_certs):
    app, _ = build_daemon(orchestrator=_orch())
    async with _serve(app, mtls_certs, frozenset({"edge"})) as ep:
        # CA-signed but CN 'edge-other' ∉ allowlist → handshake must fail.
        ctx = _client_ctx(mtls_certs, mtls_certs.client_other_cert, mtls_certs.client_other_key)
        with pytest.raises((ssl.SSLError, httpx.ConnectError, httpx.RemoteProtocolError,
                            httpx.ReadError, OSError)):
            async with httpx.AsyncClient(base_url=f"https://{ep.host}:{ep.port}", verify=ctx, timeout=5.0) as c:
                await c.get("/info")


@pytest.mark.asyncio
async def test_unset_allowlist_is_permissive(mtls_certs):
    app, _ = build_daemon(orchestrator=_orch())
    async with _serve(app, mtls_certs, None) as ep:
        ctx = _client_ctx(mtls_certs, mtls_certs.client_other_cert, mtls_certs.client_other_key)
        async with httpx.AsyncClient(base_url=f"https://{ep.host}:{ep.port}", verify=ctx, timeout=5.0) as c:
            assert (await c.get("/info")).status_code == 200
