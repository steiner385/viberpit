"""No edge path may CANCEL an in-flight daemon RPC (incident 2026-08-23, issue #734).

``asyncio.wait_for`` does not time an HTTP request out — it **cancels** it. A
``CancelledError`` is a ``BaseException``, so httpcore's ``except Exception`` in
``AnyIOStream.start_tls`` never closes the already-connected TCP stream: the socket is
orphaned for the life of the process and the pool slot is stranded permanently ACTIVE.
At ``max_connections`` every later RPC ``PoolTimeout``s and every daemon-backed route
503s — the edge wedge that needed a manual ``systemctl restart`` twice in two days.
See ``docs/superpowers/spikes/2026-08-23-edge-socket-leak.md``.

The fix is to push the deadline INTO the RPC (``DaemonClient.call(timeout=…)``) so httpx
enforces it natively and closes its own connection.

These tests assert the socket outcome from the SERVER side — no httpx/httpcore internals:
a client that cleaned up sends FIN, so the server reads EOF. A client that leaked never
does. That is exactly the control the spike used to isolate cancellation as the cause.
"""
from __future__ import annotations

import asyncio
import time
import types

import pytest
import pytest_asyncio

from server.ipc.client import DaemonClient, DaemonTimeout
from server.ipc.tls import ClientTLSFiles
from server.ipc.paths import parse_endpoint
from server.sessions.facade import call_with_deadline
from server.sessions.remote import RemoteOrchestrator

# A deadline long enough that the TCP connect completes and the TLS handshake is
# genuinely in flight when it expires, short enough to keep the suite fast.
_DEADLINE = 0.3


class _Blackhole:
    """A TCP endpoint that accepts, never speaks TLS, and never answers.

    Every RPC therefore stalls inside the TLS handshake — the exact window in which a
    cancellation orphans the socket (spike "Leak 1"). ``accepted``/``closed`` count what
    the *server* saw, so a leak is observable without touching httpx internals.
    """

    def __init__(self) -> None:
        self.accepted = 0
        self.closed = 0
        self._server: asyncio.AbstractServer | None = None
        self._writers: set = set()
        self.port = 0

    async def start(self) -> None:
        async def handle(reader: asyncio.StreamReader, writer) -> None:
            self.accepted += 1
            self._writers.add(writer)
            try:
                while await reader.read(65536):   # never respond; loop until EOF
                    pass
                self.closed += 1
            except (ConnectionError, asyncio.CancelledError):
                pass                              # torn down by stop(), not by the client
            finally:
                self._writers.discard(writer)

        self._server = await asyncio.start_server(handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        # Hang up on the sockets the client ORPHANED — otherwise their handlers never
        # return and (3.12+) Server.wait_closed() blocks on them forever.
        for writer in list(self._writers):
            writer.transport.abort()
        if self._server is not None:
            self._server.close()

    def client(self, tls: ClientTLSFiles) -> DaemonClient:
        return DaemonClient(parse_endpoint(f"https://127.0.0.1:{self.port}"), client_tls=tls)

    async def settle(self, expect: int = 1, timeout: float = 3.0) -> None:
        """Wait until every accepted connection has been closed by the client."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.accepted >= expect and self.closed >= self.accepted:
                return
            await asyncio.sleep(0.02)

    def assert_no_orphans(self) -> None:
        assert self.accepted > 0, "no RPC ever reached the endpoint — test is vacuous"
        assert self.closed == self.accepted, (
            f"{self.accepted - self.closed} of {self.accepted} daemon sockets were never "
            "closed — an in-flight RPC was cancelled instead of timed out"
        )


@pytest.fixture
def blackhole_tls(mtls_certs):
    return ClientTLSFiles(cert=mtls_certs.client_cert, key=mtls_certs.client_key,
                          ca=mtls_certs.ca)


@pytest_asyncio.fixture
async def blackhole():
    bh = _Blackhole()
    await bh.start()
    try:
        yield bh
    finally:
        await bh.stop()


def _record(session_id: str = "s1"):
    return types.SimpleNamespace(id=session_id, name="S1", repo=None, cwd=None)


# ---------------------------------------------------------------------------
# Control — why wait_for is banned around an RPC
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancelling_an_rpc_orphans_the_socket(blackhole, blackhole_tls):
    """The hazard itself, pinned. If this ever fails, httpcore fixed the BaseException
    hole in start_tls and the ban below can be relaxed — investigate, don't delete."""
    client = blackhole.client(blackhole_tls)
    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(client.call("noop"), _DEADLINE)
        await blackhole.settle()
        assert blackhole.accepted == 1
        assert blackhole.closed == 0, "cancellation is expected to orphan the socket"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_native_rpc_deadline_closes_the_socket(blackhole, blackhole_tls):
    """The control's opposite: httpx's own timeout raises an Exception, which
    start_tls DOES catch, so the stream is closed."""
    client = blackhole.client(blackhole_tls)
    try:
        with pytest.raises(DaemonTimeout):
            await client.call("noop", timeout=_DEADLINE)
        await blackhole.settle()
        blackhole.assert_no_orphans()
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# The real call sites
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enrich_one_does_not_orphan_daemon_sockets(blackhole, blackhole_tls):
    """GET /api/sessions fans out 3 RPCs per session — the wedge's trigger."""
    from server.api.sessions import _enrich_one

    client = blackhole.client(blackhole_tls)
    orch = RemoteOrchestrator(client)
    try:
        repo, sugg, prs, summary = await _enrich_one(
            orch, _record(), viewing=False, timeout=_DEADLINE)
        # Degradation contract is unchanged: empty fields, never a raised error.
        assert sugg == [] and prs == [] and summary == ""
        await blackhole.settle(expect=3)
        blackhole.assert_no_orphans()
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_safe_usage_does_not_orphan_daemon_sockets(blackhole, blackhole_tls,
                                                         monkeypatch):
    """GET /api/accounts fans out one usage RPC per account."""
    from server.api import accounts as acct_api

    monkeypatch.setattr(acct_api, "_local_cached_usage", lambda name: None)
    client = blackhole.client(blackhole_tls)
    orch = RemoteOrchestrator(client)
    try:
        assert await acct_api._safe_usage(orch, "acct", _DEADLINE) is None
        await blackhole.settle()
        blackhole.assert_no_orphans()
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_ws_resume_deadline_does_not_orphan_daemon_sockets(blackhole, blackhole_tls):
    """ws.py bounds ensure_driver by _RESUME_TIMEOUT through the same helper, and maps
    the expiry to a DaemonTimeout (→ SESSION_RESUME_TIMEOUT), not a bare 1013 close."""
    client = blackhole.client(blackhole_tls)
    orch = RemoteOrchestrator(client)
    try:
        with pytest.raises(DaemonTimeout):
            await call_with_deadline(orch.ensure_driver, "s1", "ident", timeout=_DEADLINE)
        await blackhole.settle()
        blackhole.assert_no_orphans()
    finally:
        await client.aclose()
