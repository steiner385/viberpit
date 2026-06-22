# server/tests/test_remote_mode_edge.py
import asyncio
import os
import threading
from unittest import mock

import pytest
from starlette.testclient import TestClient

from server.app import create_app
from server.ipc.daemon import create_daemon_app
from server.ipc.orchestrator_rpc import register_orchestrator_rpc
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.types import SessionRecord
from server.tests.ipc_harness import serve_daemon_uds

_IDENT = {"Tailscale-User-Login": "edge@example.com"}


def _rec(sid: str, status: str = "idle") -> SessionRecord:
    r = SessionRecord(id=sid, name=sid.upper(), account="acct",
                      claude_session_id="cs-" + sid, cols=80, rows=24, cwd="/tmp")
    r.status = status
    return r


class _DaemonThread:
    """Run a daemon app on a real UDS in a background thread/loop. Real socket =
    the edge (its own loop) can reach it cross-thread, as in production."""

    def __init__(self, orch):
        self._orch = orch
        self.sock = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._err = None
        self._t = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def _serve():
            dapp = create_daemon_app()
            register_orchestrator_rpc(dapp, self._orch)  # running loop → pump starts
            async with serve_daemon_uds(dapp) as sock:
                self.sock = sock
                self._ready.set()
                while not self._stop.is_set():
                    await asyncio.sleep(0.05)

        try:
            loop.run_until_complete(_serve())
        except Exception as exc:  # noqa: BLE001 — surface to the main thread
            self._err = exc
            self._ready.set()
        finally:
            # Cancel any background daemon tasks (e.g. the orchestrator global-pump)
            # and let them settle before closing the loop, else a still-scheduled
            # task raises "Event loop is closed" on GC (PytestUnraisableException).
            pending = asyncio.all_tasks(loop)
            for t in pending:
                t.cancel()
            if pending:
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            loop.close()

    def start(self):
        self._t.start()
        if not self._ready.wait(5):
            raise RuntimeError("daemon thread did not start")
        if self._err is not None:
            raise self._err

    def stop(self):
        self._stop.set()
        self._t.join(5)


@pytest.fixture()
def remote_edge(tmp_path):
    orch = SessionOrchestrator(sdk_factory=mock.MagicMock(), poll_interval=9999.0,
                               usage_fn=lambda n: None)
    daemon = _DaemonThread(orch)
    daemon.start()
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'edge.db'}"
    env = {
        "CCSWITCH_SESSIOND_SOCKET": daemon.sock,
        "CCSWITCH_DB_URL": db_url,
        "CCSWITCH_TAILNET_ALLOWLIST": _IDENT["Tailscale-User-Login"],
    }
    try:
        with mock.patch.dict(os.environ, env):
            app = create_app()
            with TestClient(app, raise_server_exceptions=False) as client:
                yield orch, client
    finally:
        daemon.stop()


@pytest.fixture()
def dead_socket_edge(tmp_path):
    """Edge in remote mode pointed at a socket with no daemon → every RPC raises
    DaemonUnavailable."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'edge.db'}"
    env = {
        "CCSWITCH_SESSIOND_SOCKET": str(tmp_path / "dead.sock"),
        "CCSWITCH_DB_URL": db_url,
        "CCSWITCH_TAILNET_ALLOWLIST": _IDENT["Tailscale-User-Login"],
    }
    with mock.patch.dict(os.environ, env):
        app = create_app()
        with TestClient(app, raise_server_exceptions=False) as client:
            yield client


def test_healthz_busy_count_over_rpc(remote_edge):
    orch, client = remote_edge
    # Two active sessions on the daemon → edge healthz reflects them over RPC.
    orch._records["a"] = _rec("a", status="in_progress")
    orch._records["b"] = _rec("b", status="in_progress")
    resp = client.get("/healthz")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["activeSessions"] == 2
    assert body["busy"] is True
    assert body["daemon"] == "ok"


def test_rest_call_returns_503_when_daemon_down(dead_socket_edge):
    # GET /api/sessions → facade.list_sessions() → DaemonUnavailable → 503.
    resp = dead_socket_edge.get("/api/sessions", headers=_IDENT)
    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"] == "session daemon unavailable"
