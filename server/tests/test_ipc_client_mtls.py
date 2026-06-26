from unittest import mock

import pytest

from server.ipc.client import DaemonClient, DaemonUnavailable
from server.ipc.serve import build_daemon
from server.ipc.tls import ClientTLSFiles
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.types import SessionRecord
from server.tests.ipc_harness import serve_daemon_tls


def _orch():
    o = SessionOrchestrator(sdk_factory=mock.MagicMock(), poll_interval=9999.0,
                            usage_fn=lambda n: None)
    o._records["s1"] = SessionRecord(id="s1", name="S1", account="acct",
                                     claude_session_id="cs", cols=80, rows=24, cwd="/tmp")
    return o


def _good_tls(c):
    return ClientTLSFiles(cert=c.client_cert, key=c.client_key, ca=c.ca)


@pytest.mark.asyncio
async def test_rpc_and_events_over_mtls(mtls_certs):
    app, _ = build_daemon(orchestrator=_orch())
    async with serve_daemon_tls(app, mtls_certs) as ep:
        client = DaemonClient(ep, client_tls=_good_tls(mtls_certs))
        try:
            info = await client.info()
            assert info["protocol"] == 1
            listed = await client.call("list_sessions")
            assert [r["id"] for r in listed["sessions"]] == ["s1"]
            # event stream round-trips over wss
            gen = client.subscribe("demo")
            await client.events_ready("demo")
            await client.call("echo", stream="demo", text="hi")
            evt = await gen.__anext__()
            assert evt["text"] == "hi"
        finally:
            await client.aclose()


@pytest.mark.asyncio
async def test_rpc_rejected_without_client_cert(mtls_certs):
    app, _ = build_daemon(orchestrator=_orch())
    async with serve_daemon_tls(app, mtls_certs) as ep:
        # Trust server CA but present no client cert (use a CA-only context via a bogus pair).
        bad = ClientTLSFiles(cert=mtls_certs.rogue_client_cert,
                             key=mtls_certs.rogue_client_key, ca=mtls_certs.ca)
        client = DaemonClient(ep, client_tls=bad)
        try:
            with pytest.raises((DaemonUnavailable, Exception)):
                await client.info()
        finally:
            await client.aclose()


def test_str_path_back_compat_is_unix():
    c = DaemonClient("/tmp/x.sock")
    assert c.endpoint.is_unix and c.endpoint.path == "/tmp/x.sock"
