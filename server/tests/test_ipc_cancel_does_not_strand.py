"""No edge path may CANCEL an in-flight daemon RPC (incident 2026-08-23, issue #734).

``asyncio.wait_for`` does not time an HTTP request out — it **cancels** it, throwing a
``CancelledError`` (a ``BaseException``) into httpcore. Measured consequence: the socket is
orphaned for the life of the process and its pool slot is never reclaimed. At
``max_connections`` every later RPC ``PoolTimeout``s and every daemon-backed route 503s —
the edge wedge that needed a manual ``systemctl restart`` twice in two days.
See ``docs/superpowers/spikes/2026-08-23-edge-socket-leak.md``.

WHAT IS AND IS NOT ESTABLISHED. The spike's controlled experiment — same server, same
client, same stall, varying only cancel-vs-timeout — is solid: cancellation leaked, httpx's
own timeout leaked zero. WHERE inside httpcore the cancel does this is NOT established. Its
first explanation (``AnyIOStream.start_tls``'s ``except Exception``) does not survive a read
of httpcore 1.0.9: a cancel inside ``_connect`` is caught by
``AsyncHTTPConnection.handle_async_request``'s ``except BaseException`` → ``_connect_failed
= True`` → ``is_closed()`` True → evictable. The shape that DOES survive is a pooled
connection object created but never dispatched on (``_connection is None`` and
``_connect_failed`` False, so nothing reports it closed, expired, idle or available).

That correction has teeth here: the refuted explanation implied the unix-socket branch
could not strand, because it runs no TLS handshake. The surviving shape needs no handshake
at all, so that inference is unsupported. The ``blackhole`` fixture is therefore
PARAMETERISED over a TCP/TLS endpoint and a unix-socket one, and every call-site test runs
on both — the ban on cancelling an RPC is a property of the code, not of the transport, and
nothing here may encode "unix is exempt".

The one test that is TLS-only is ``test_cancelling_an_rpc_orphans_the_socket``, which pins
the HAZARD rather than the fix. It is TLS-only because that is where the leak reproduces:
measured over unix, a cancel in the same window cleans up (see the spike's amendment table).
That asymmetry is a fact about what has been reproduced, NOT a claim that the unix branch
cannot strand.

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

    Every RPC therefore stalls inside the TLS handshake — the window in which a
    cancellation is known to orphan the socket. ``accepted``/``closed`` count what the
    *server* saw, so a leak is observable without touching httpx internals.
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


class _UnixBlackhole(_Blackhole):
    """The same endpoint over a unix socket — no TLS anywhere in the path.

    Exists so every call-site test below runs on BOTH transports. The refuted
    ``start_tls`` mechanism would make this variant vacuous ("no handshake, no leak");
    the shape that survives source reading is pool bookkeeping and is transport-agnostic,
    so the unix branch gets the same coverage rather than an exemption.
    """

    def __init__(self, path: str) -> None:
        super().__init__()
        self.path = path

    async def start(self) -> None:
        async def handle(reader: asyncio.StreamReader, writer) -> None:
            self.accepted += 1
            self._writers.add(writer)
            try:
                while await reader.read(65536):   # never respond; loop until EOF
                    pass
                self.closed += 1
            except (ConnectionError, asyncio.CancelledError):
                pass
            finally:
                self._writers.discard(writer)

        self._server = await asyncio.start_unix_server(handle, self.path)

    def client(self, tls: ClientTLSFiles) -> DaemonClient:
        return DaemonClient(parse_endpoint(f"unix://{self.path}"))


@pytest.fixture
def blackhole_tls(mtls_certs):
    return ClientTLSFiles(cert=mtls_certs.client_cert, key=mtls_certs.client_key,
                          ca=mtls_certs.ca)


@pytest_asyncio.fixture
async def tls_blackhole():
    """TLS-only, for the HAZARD control — the one place the leak reproduces."""
    bh = _Blackhole()
    await bh.start()
    try:
        yield bh
    finally:
        await bh.stop()


@pytest_asyncio.fixture(params=["tls", "unix"])
async def blackhole(request, tmp_path):
    bh = (_Blackhole() if request.param == "tls"
          else _UnixBlackhole(str(tmp_path / "bh.sock")))
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
async def test_cancelling_an_rpc_orphans_the_socket(tls_blackhole, blackhole_tls):
    """The hazard itself, pinned — over TLS, where it reproduces.

    If this ever fails, httpcore closed whatever hole this rides on and the ban below can
    be relaxed — investigate, don't delete. Deliberately NOT parameterised over unix:
    measured over a unix socket, a cancel in this window cleans up. That is a fact about
    what reproduces, not a licence to cancel RPCs on the unix branch — see the module
    docstring and the spike's "Where inside httpcore" amendment.
    """
    blackhole = tls_blackhole
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
async def test_enrich_batch_does_not_orphan_daemon_sockets(blackhole, blackhole_tls):
    """The batched path must clean up exactly as the per-session path does — and cost
    three sockets for the whole page rather than three per session (#742)."""
    from server.api.sessions import _enrich_batch

    client = blackhole.client(blackhole_tls)
    orch = RemoteOrchestrator(client)
    try:
        records = [_record("s1"), _record("s2")]
        repos, suggs, prs, summaries = await _enrich_batch(
            orch, records, viewing=False, timeout=_DEADLINE)
        assert list(suggs) == [[], []]
        assert list(prs) == [[], []]
        assert list(summaries) == ["", ""]
        # Three RPCs for two sessions — six would mean the batch did not take effect.
        # settle() only waits for >= expect, so the count itself needs its own assertion:
        # six accepted sockets would satisfy the wait just as well as three.
        await blackhole.settle(expect=3)
        assert blackhole.accepted == 3, (
            f"expected exactly 3 batched RPCs for 2 sessions, saw {blackhole.accepted}"
        )
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
        assert await acct_api._safe_usage(orch, "acct", _DEADLINE) == (None, None)
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


# ---------------------------------------------------------------------------
# The deadline-capable set is declared, not inferred
# ---------------------------------------------------------------------------

# Methods that promise "timeout= is MY RPC deadline, and I forward it to httpx".
# call_with_deadline hands the deadline straight to these; everything else is bounded
# from outside with wait_for. Changing this set changes how a deadline is applied, so it
# is pinned here: add or remove an @rpc_deadline and this test tells you to say why.
_DEADLINE_CAPABLE_RPCS = frozenset({
    "ensure_driver",
    "latest_suggestions",
    "latest_suggestions_many",
    "latest_summary",
    "latest_summary_many",
    "prs_for",
    "prs_for_many",
    "usage",
})


def _marked() -> set[str]:
    import inspect as _inspect
    return {name for name, fn in _inspect.getmembers(RemoteOrchestrator, _inspect.isfunction)
            if getattr(fn, "__ccswitch_rpc_deadline__", False)}


def test_marked_rpcs_match_the_allowlist():
    assert _marked() == set(_DEADLINE_CAPABLE_RPCS)


def test_no_rpc_takes_a_timeout_without_being_marked():
    """The dangerous direction: an RPC that accepts ``timeout=`` but carries no marker
    would be bounded by wait_for instead — i.e. cancelled mid-flight, re-introducing the
    strand. Marking it (or renaming the parameter, as ``transcript_window``'s
    ``timeout_s`` does) is the fix."""
    import inspect as _inspect

    unmarked = set()
    for name, fn in _inspect.getmembers(RemoteOrchestrator, _inspect.isfunction):
        if getattr(fn, "__ccswitch_rpc_deadline__", False):
            continue
        try:
            params = _inspect.signature(fn).parameters
        except (TypeError, ValueError):  # pragma: no cover - defensive
            continue
        if "timeout" in params:
            unmarked.add(name)
    assert unmarked == set(), (
        f"{sorted(unmarked)} accept timeout= but are not @rpc_deadline — "
        "call_with_deadline would cancel them instead of pushing the deadline in")


@pytest.mark.asyncio
async def test_an_unrelated_timeout_kwarg_is_never_hijacked():
    """The failure the marker exists to prevent: a callee whose ``timeout=`` means
    something else entirely must NOT receive the RPC deadline as that argument, and must
    still be bounded from outside."""
    class _UnrelatedTimeout:
        def __init__(self) -> None:
            self.seen = "not-called"

        async def latest_summary(self, session_id, *, viewing=False, timeout=None):
            self.seen = timeout          # a poll interval, say — nothing to do with RPCs
            await asyncio.sleep(30)

    obj = _UnrelatedTimeout()
    with pytest.raises(asyncio.TimeoutError):
        await call_with_deadline(obj.latest_summary, "s1", viewing=False, timeout=0.05)
    assert obj.seen is None, "the RPC deadline leaked into an unrelated timeout parameter"
