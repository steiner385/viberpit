"""The edge's daemon client must be explicitly bounded (issue #734).

It used to pass a bare ``timeout=30.0`` and no ``limits=``. httpx expands a scalar
timeout to connect = read = write = **pool** = 30s, and defaults to
``max_connections=100``. So a request that could not get a pool slot waited the full
30s before failing — which is exactly what every daemon-backed route did while the pool
held stranded connections: "503 after exactly 30 seconds" on every RPC route while
no-RPC routes answered in 10ms.

Both transports get identical bounds: the unix branch is what tests and a local daemon
use, so leaving it unbounded would hide regressions from CI.
"""
from __future__ import annotations

import httpx
import pytest

from server.ipc.client import _LIMITS, _TIMEOUT, DaemonClient
from server.ipc.tls import ClientTLSFiles


def _unix_client(tmp_path) -> DaemonClient:
    return DaemonClient(f"unix://{tmp_path}/daemon.sock")


def _tls_client(mtls_certs) -> DaemonClient:
    return DaemonClient("https://127.0.0.1:9443",
                        client_tls=ClientTLSFiles(cert=mtls_certs.client_cert,
                                                  key=mtls_certs.client_key,
                                                  ca=mtls_certs.ca))


def _pool(client: DaemonClient):
    return client._http._transport._pool


@pytest.fixture(params=["unix", "tls"])
def client(request, tmp_path, mtls_certs):
    c = _unix_client(tmp_path) if request.param == "unix" else _tls_client(mtls_certs)
    yield c
    # Nothing was ever connected, so aclose() needs no running loop work.


def test_timeout_is_granular_not_scalar(client):
    """A scalar timeout sets the POOL leg too — that is the 30s 503."""
    timeout = client._http.timeout
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.pool is not None, "pool acquisition must have its own budget"
    assert timeout.read is not None and timeout.connect is not None


def test_pool_acquisition_fails_fast(client):
    """Starved of a connection, a caller must fail fast rather than tie up a request
    for the whole read budget."""
    timeout = client._http.timeout
    assert timeout.pool <= 10.0, f"pool wait of {timeout.pool}s is not fail-fast"
    assert timeout.pool < timeout.read, (
        "waiting for a slot must be cheaper than waiting for a slow-but-healthy RPC")


def test_pool_acquisition_outlasts_the_slowest_rpc(client):
    """...but "fail fast" must not mean "shed load the pool would have served".

    A queued caller is waiting for ONE in-flight RPC to return its slot, and
    ``transcript_events`` was measured at 4.3s. With the leg below that, two concurrent
    dashboard loads (~63 RPCs each against 100 slots) failed ~26 RPCs with empty
    enrichment fields against a perfectly healthy daemon — and raised the pool-exhaustion
    diagnostic while doing it.
    """
    assert client._http.timeout.pool > 4.3, (
        "the pool leg is shorter than the slowest known RPC — a fan-out queue is shed "
        "rather than served")


def test_a_per_call_deadline_spends_its_whole_budget_on_a_slot():
    """A caller's own deadline is the floor for the pool leg, not just the ceiling.

    An enrichment RPC declares a 3s budget; with a fixed 2s leg it was abandoned in the
    queue a full second before the caller would have given up. Waiting longer than the
    caller's budget is pointless, but waiting LESS throws away a request the caller was
    still willing to wait for.
    """
    from server.ipc.client import _call_timeout

    assert _call_timeout(3.0).pool == 3.0
    # ...and the ceiling still applies to a long deadline.
    assert _call_timeout(90.0).pool == _TIMEOUT.pool


def test_read_budget_stays_generous(client):
    """Some RPCs legitimately take seconds (transcript_events measured at 4.3s)."""
    assert client._http.timeout.read >= 10.0


def test_limits_are_explicit(client):
    """Wired from the module constant, not left to whatever httpx defaults to — so the
    fan-out ceiling is a deliberate number that reviewers can see and change."""
    pool = _pool(client)
    assert pool._max_connections == _LIMITS.max_connections
    assert pool._max_keepalive_connections == _LIMITS.max_keepalive_connections
    assert pool._keepalive_expiry == _LIMITS.keepalive_expiry
    assert _LIMITS.max_keepalive_connections <= _LIMITS.max_connections


def test_keepalive_expiry_is_bounded(client):
    """An idle connection to a daemon that has since restarted must be discarded,
    not handed out to the next caller."""
    expiry = _pool(client)._keepalive_expiry
    assert expiry is not None and 0 < expiry <= 30.0


def test_both_transports_share_one_definition(tmp_path, mtls_certs):
    """The unix and TLS constructions must not drift apart."""
    unix, tls = _unix_client(tmp_path), _tls_client(mtls_certs)
    assert unix._http.timeout == tls._http.timeout == _TIMEOUT
    for attr in ("_max_connections", "_max_keepalive_connections", "_keepalive_expiry"):
        assert getattr(_pool(unix), attr) == getattr(_pool(tls), attr), attr


def test_per_call_deadline_does_not_stretch_the_pool_leg():
    """A long per-call timeout (automation_* passes 90s) must bound the RPC, not the
    wait for a connection slot — a scalar would set both."""
    from server.ipc.client import _call_timeout

    expanded = _call_timeout(90.0)
    assert expanded.read == 90.0 and expanded.write == 90.0
    assert expanded.pool == _TIMEOUT.pool
    assert expanded.connect <= _TIMEOUT.connect


def test_per_call_deadline_shorter_than_the_default_tightens_connect():
    """A tight per-call budget (usage passes 3s) must not be allowed to spend more
    than that budget on the connect leg alone."""
    from server.ipc.client import _call_timeout

    expanded = _call_timeout(1.0)
    assert expanded.connect == 1.0 and expanded.read == 1.0
