"""Per-call httpx clients in the deploy controller and session daemon must not grow the heap.

Every httpx client built with the default ``verify=True`` loads the certifi CA bundle
into a fresh ``SSLContext``. Under OpenSSL 3.5 + glibc, each such client that makes a
request leaves ~0.7 MB of freed-but-unreusable heap behind (in-use memory stays flat,
so tracemalloc sees nothing). The controller built one per 30 s detect tick
(``edge_live_sha`` -> ``steps._default_fetch``), which reached VmSize 4.7 GB, mostly
swapped out, over 11 days on dobby (2026-11-24). A unix-socket ``DaemonClient`` built
two per call even though a UDS never speaks TLS, and the daemon's DeployWatcher
built one per health URL every 30 s.

These tests drive the REAL production fetch paths against local servers and measure
glibc's own heap accounting, the only instrument that sees this growth.
"""
from __future__ import annotations

import asyncio
import ctypes
import ctypes.util
import functools
import json
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from server.deploy import detect, steps

# Pre-fix: ~0.68 MB per call, so N calls grow the heap by ~N * 0.68 MB (~136 MB here).
# Post-fix the growth is well under 1 MB. The bound sits far from both.
N_CALLS = 200
MAX_GROWTH_MB = 32.0


class _MallInfo2(ctypes.Structure):
    _fields_ = [(n, ctypes.c_size_t) for n in (
        "arena", "ordblks", "smblks", "hblks", "hblkhd", "usmblks", "fsmblks",
        "uordblks", "fordblks", "keepcost")]


def _libc_mallinfo2():
    path = ctypes.util.find_library("c")
    libc = ctypes.CDLL(path) if path else None
    fn = getattr(libc, "mallinfo2", None) if libc is not None else None
    if fn is None:
        pytest.skip("needs glibc >= 2.33 (mallinfo2) to measure heap retention")
    fn.restype = _MallInfo2
    return fn


def _heap_mb(mallinfo2) -> float:
    """Bytes glibc holds from the OS for the heap (all arenas, plus mmapped chunks)."""
    m = mallinfo2()
    return (m.arena + m.hblkhd) / 2**20


_HEALTHZ = json.dumps({"status": "ok", "sha": "abc1234"}).encode()
_INFO = json.dumps({"sha": "abc1234", "checkoutSha": "abc1234", "protocol": 1}).encode()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802 — http.server's naming
        body = _INFO if self.path == "/info" else _HEALTHZ
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # quiet
        pass

    def address_string(self):  # a unix socket has no (host, port) peer
        return "local"


@pytest.fixture
def tcp_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}/healthz"
    finally:
        srv.shutdown()
        srv.server_close()


class _UnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


@pytest.fixture
def uds_server(tmp_path):
    path = str(tmp_path / "d.sock")
    srv = _UnixHTTPServer(path, _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield path
    finally:
        srv.shutdown()
        srv.server_close()


class _FakeGit:
    """Edge already on origin/master, so a tick is exactly fetch + rev-parse + the
    edge /healthz read — the steady state the controller sits in for days."""

    def fetch(self, repo, ref="master", run=None):
        pass

    def current_sha(self, repo, run=None):
        return "abc1234" + "0" * 33

    def remote_sha(self, repo, ref="origin/master", run=None):
        return "abc1234" + "0" * 33

    def short_sha(self, repo, sha, run=None):
        return sha[:7]

    def log_range(self, repo, a, b, run=None):
        return []


def test_detect_poll_cycles_keep_heap_bounded(tcp_server):
    mallinfo2 = _libc_mallinfo2()
    # The production wiring from app.build_controller_app, minus git: the live sha
    # comes from the edge's /healthz through steps' default fetch.
    compute = functools.partial(
        detect.compute_pending, git_mod=_FakeGit(), ci_fn=lambda sha: "green",
        live_sha_fn=functools.partial(detect.edge_live_sha, tcp_server))
    loop = detect.DetectLoop("/repo", interval_s=0, compute=compute)

    async def run(n):
        for _ in range(n):
            await loop.tick()

    asyncio.run(run(5))   # warm up: imports, the executor thread, first allocations
    assert loop.state.live_sha == "abc1234", "the tick must really read the edge"
    before = _heap_mb(mallinfo2)
    asyncio.run(run(N_CALLS))
    growth = _heap_mb(mallinfo2) - before
    assert loop.state.live_sha == "abc1234"
    assert growth < MAX_GROWTH_MB, (
        f"heap grew {growth:.1f} MB over {N_CALLS} detect ticks "
        f"(~{growth / N_CALLS * 1024:.0f} KB/tick): a per-call httpx client is "
        "loading its own CA bundle again — pass server.http_tls.shared_ssl_context()")


def test_status_fetch_keeps_heap_bounded(tcp_server):
    """``require_endpoints_healthy``'s probe, polled during every deploy."""
    mallinfo2 = _libc_mallinfo2()
    for _ in range(5):
        assert steps._default_status_fetch(tcp_server) == 200
    before = _heap_mb(mallinfo2)
    for _ in range(N_CALLS):
        steps._default_status_fetch(tcp_server)
    growth = _heap_mb(mallinfo2) - before
    assert growth < MAX_GROWTH_MB, f"heap grew {growth:.1f} MB over {N_CALLS} probes"


def test_unix_daemon_client_keeps_heap_bounded(uds_server):
    """A fresh DaemonClient per call is the runners phase's pattern (_rpc, _verify
    polling every 3 s for up to 180 s, host_probe.default_fetch)."""
    from server.sessions.host_probe import default_fetch

    mallinfo2 = _libc_mallinfo2()
    endpoint = f"unix://{uds_server}"

    async def run(n):
        for _ in range(n):
            info = await default_fetch(endpoint)
        return info

    assert asyncio.run(run(5))["sha"] == "abc1234"
    before = _heap_mb(mallinfo2)
    asyncio.run(run(N_CALLS))
    growth = _heap_mb(mallinfo2) - before
    assert growth < MAX_GROWTH_MB, (
        f"heap grew {growth:.1f} MB over {N_CALLS} unix-socket DaemonClient calls")


def test_daemon_deploy_watch_poll_keeps_heap_bounded(tcp_server, monkeypatch):
    """The session daemon's DeployWatcher GETs every configured healthUrl every 30 s
    (and the delivery-health refresh reuses the same fetch)."""
    from server.pipeline import deploy_watch

    # The SSRF guard refuses loopback; the local test server is the only target here.
    monkeypatch.setattr(deploy_watch, "_url_allowed", lambda url: True)
    mallinfo2 = _libc_mallinfo2()

    async def run(n):
        for _ in range(n):
            resp = await deploy_watch._default_http_get(tcp_server)
        return resp

    assert asyncio.run(run(5)).status_code == 200
    before = _heap_mb(mallinfo2)
    asyncio.run(run(N_CALLS))
    growth = _heap_mb(mallinfo2) - before
    assert growth < MAX_GROWTH_MB, (
        f"heap grew {growth:.1f} MB over {N_CALLS} deploy-watch health polls")
