"""Production entrypoint plumbing for the session daemon: build the app with a real
orchestrator, run its lifecycle, and serve it on a Unix socket or mTLS TCP port."""
import asyncio
import logging
import os
import ssl
from contextlib import asynccontextmanager

import uvicorn

from server.config import get_settings
from server.db import init_db
from server.ipc.daemon import create_daemon_app
from server.ipc.orchestrator_rpc import register_orchestrator_rpc
from server.ipc.x11_rpc import register_x11
from server.ipc.paths import Endpoint, sessiond_endpoint
from server.ipc.tls import DaemonTLSFiles, daemon_tls_files
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.x11manager import build_x11_manager

log = logging.getLogger("server.ipc.serve")

# Cap on the daemon's display-teardown sweep at shutdown — stays well under systemd's
# TimeoutStopSec so graceful cleanup completes instead of being SIGKILLed (issue #206).
_DAEMON_SHUTDOWN_SWEEP_TIMEOUT_S = 15.0


def uvicorn_run_kwargs(endpoint: Endpoint, *, daemon_tls: DaemonTLSFiles | None = None) -> dict:
    """Build uvicorn bind kwargs for a daemon endpoint.

    unix  -> {uds}.
    https -> {host, port, ssl_*}. ssl_cert_reqs=CERT_REQUIRED makes it MUTUAL TLS:
             a client without a CA-signed cert fails the handshake.
    """
    if endpoint.is_unix:
        return {"uds": endpoint.path}
    files = daemon_tls or daemon_tls_files()
    return {
        "host": endpoint.host,
        "port": endpoint.port,
        "ssl_certfile": files.cert,
        "ssl_keyfile": files.key,
        "ssl_ca_certs": files.ca,
        "ssl_cert_reqs": ssl.CERT_REQUIRED,
    }


def build_daemon(orchestrator=None):
    """Build the daemon app with a real orchestrator + x11 manager + the RPC surface.
    Mirrors the edge's construction (server/app.py) so the daemon owns sessions identically."""
    app = create_daemon_app()
    if orchestrator is None:
        orchestrator = SessionOrchestrator()
    orchestrator._x11 = build_x11_manager(get_settings())
    register_orchestrator_rpc(app, orchestrator)
    app.state.x11 = orchestrator._x11
    register_x11(app, app.state.x11)
    return app, orchestrator


@asynccontextmanager
async def daemon_lifespan(app, orchestrator):
    """Daemon startup/shutdown: init the shared DB, start the orchestrator (poller +
    rotation + global pump), and stop it cleanly. Rehydrate is lazy — no driver spawned
    at startup, so a daemon start does not respawn every session at once."""
    db_url = os.environ.get("CCSWITCH_DB_URL", "sqlite+aiosqlite:///:memory:")
    await init_db(db_url=db_url)
    try:
        await orchestrator.rehydrate()
    except Exception:
        log.error("daemon rehydrate() failed — starting with no sessions restored", exc_info=True)
    try:
        await orchestrator.rearm_background_tasks()
    except Exception:
        log.error("daemon rearm_background_tasks() failed", exc_info=True)
    await orchestrator.start()
    # Sweep stale X lock/socket files AND reap orphaned Xvfb/x11vnc/chrome left by a
    # prior SIGKILLed run (issue #206, startup half). The daemon owns the displays in
    # remote mode yet swept nothing at boot, so orphans accumulated across restarts.
    x11 = getattr(orchestrator, "_x11", None)
    if x11 is not None:
        try:
            n = x11.sweep_stale()
            if n:
                log.info("daemon startup: swept %d stale X lock/socket(s)", n)
        except Exception:
            log.warning("daemon startup: X sweep failed", exc_info=True)
        try:
            k = x11.sweep_orphan_processes()
            if k:
                log.info("daemon startup: reaped %d orphaned X display process(es)", k)
        except Exception:
            log.warning("daemon startup: X orphan reap failed", exc_info=True)
    try:
        yield
    finally:
        # Tear down every virtual display before stopping (issue #206, daemon half).
        # orchestrator.stop() only cancels background tasks — it never freed displays,
        # so the daemon (which owns sessions in remote mode) leaked Xvfb/x11vnc/chrome
        # on every restart. stop_all() tears them down CONCURRENTLY; the wait_for cap
        # keeps it well inside systemd's TimeoutStopSec (mirrors the edge sweep).
        x11 = getattr(orchestrator, "_x11", None)
        if x11 is not None:
            try:
                await asyncio.wait_for(x11.stop_all(), timeout=_DAEMON_SHUTDOWN_SWEEP_TIMEOUT_S)
            except Exception:
                log.warning("daemon shutdown: x11 teardown failed", exc_info=True)
        await orchestrator.stop()


def serve(socket_path: str | None = None) -> None:
    # Back-compat: an explicit socket_path arg forces a unix bind at that path.
    endpoint = Endpoint("unix", path=socket_path) if socket_path else sessiond_endpoint()
    app, orchestrator = build_daemon()
    app.router.lifespan_context = lambda _app: daemon_lifespan(_app, orchestrator)
    where = endpoint.path if endpoint.is_unix else f"{endpoint.host}:{endpoint.port} (mTLS)"
    log.info("session daemon serving on %s", where)
    uvicorn.run(app, log_level="info", **uvicorn_run_kwargs(endpoint))
