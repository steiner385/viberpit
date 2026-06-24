"""Production entrypoint plumbing for the session daemon: build the app with a real
orchestrator, run its lifecycle, and serve it on a Unix socket."""
import logging
import os
from contextlib import asynccontextmanager

import uvicorn

from server.config import get_settings
from server.db import init_db
from server.ipc.daemon import create_daemon_app
from server.ipc.orchestrator_rpc import register_orchestrator_rpc
from server.ipc.x11_rpc import register_x11
from server.ipc.paths import sessiond_socket
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.x11manager import build_x11_manager

log = logging.getLogger("server.ipc.serve")


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
    try:
        yield
    finally:
        # Tear down every virtual display before stopping — bounded + concurrent so
        # the daemon finishes within systemd's TimeoutStopSec and never leaks
        # Xvfb/x11vnc/chrome on restart (#206). orchestrator.stop() only cancels
        # background tasks, so without this the daemon-owned displays leak.
        x11 = getattr(orchestrator, "_x11", None)
        if x11 is not None:
            try:
                await x11.shutdown_all()
            except Exception:
                log.warning("daemon shutdown: x11 teardown failed", exc_info=True)
        await orchestrator.stop()


def serve(socket_path: str | None = None) -> None:
    sock = socket_path or sessiond_socket()
    app, orchestrator = build_daemon()
    app.router.lifespan_context = lambda _app: daemon_lifespan(_app, orchestrator)
    log.info("session daemon serving on %s", sock)
    uvicorn.run(app, uds=sock, log_level="info")
