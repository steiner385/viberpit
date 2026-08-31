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
from server.ipc.drain_on_signal import install_drain_on_sigterm
from server.ipc.orchestrator_rpc import register_orchestrator_rpc
from server.ipc.reauth_rpc import register_reauth
from server.ipc.x11_rpc import register_x11
from server.reauth import ReauthManager
from server.ipc.paths import Endpoint, sessiond_endpoint
from server.ipc.tls import DaemonTLSFiles, daemon_tls_files, daemon_ssl_context_factory, client_allowlist
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.x11manager import build_x11_manager

log = logging.getLogger("server.ipc.serve")

# Cap on the daemon's display-teardown sweep at shutdown — stays well under systemd's
# TimeoutStopSec so graceful cleanup completes instead of being SIGKILLed (issue #206).
_DAEMON_SHUTDOWN_SWEEP_TIMEOUT_S = 15.0

# Bounds how long uvicorn waits for open connections (edge<->daemon RPC/WS traffic)
# before forcing them shut on SIGTERM. Without this, graceful shutdown waits
# indefinitely, the daemon_lifespan() shutdown branch below (x11 teardown, reauth
# shutdown, orchestrator stop) never runs, and systemd SIGKILLs at TimeoutStopSec
# on every deploy (transcript audit V4 — same defect as the edge's ccswitch.service).
_DAEMON_GRACEFUL_SHUTDOWN_TIMEOUT_S = 10


def uvicorn_run_kwargs(endpoint: Endpoint, *, daemon_tls: DaemonTLSFiles | None = None,
                       allowlist: frozenset[str] | None = None) -> dict:
    """Build uvicorn bind kwargs for a daemon endpoint.

    unix  -> {uds}.
    https -> {host, port, ssl_*}. ssl_cert_reqs=CERT_REQUIRED makes it MUTUAL TLS:
             a client without a CA-signed cert fails the handshake.
    When allowlist is truthy, adds ssl_context_factory to enforce CN/SAN identity checks.
    """
    if endpoint.is_unix:
        return {"uds": endpoint.path}
    files = daemon_tls or daemon_tls_files()
    kwargs = {
        "host": endpoint.host,
        "port": endpoint.port,
        "ssl_certfile": files.cert,
        "ssl_keyfile": files.key,
        "ssl_ca_certs": files.ca,
        "ssl_cert_reqs": ssl.CERT_REQUIRED,
    }
    if allowlist:
        kwargs["ssl_context_factory"] = daemon_ssl_context_factory(allowlist)
    return kwargs


def build_daemon(orchestrator=None):
    """Build the daemon app with a real orchestrator + x11 manager + the RPC surface.
    Mirrors the edge's construction (server/app.py) so the daemon owns sessions identically."""
    app = create_daemon_app()
    if orchestrator is None:
        orchestrator = SessionOrchestrator()
    orchestrator._x11 = build_x11_manager(get_settings())
    from server.sessions.copilot_proxy import build_copilot_proxy_manager
    from server.sessions.copilot_auth import CopilotAuthManager
    from ccswitch import paths as _ccswitch_paths
    from ccswitch.accounts import set_copilot_usage_reader
    orchestrator._copilot = build_copilot_proxy_manager(get_settings())
    orchestrator._copilot_auth = CopilotAuthManager(
        token_dir_for=_ccswitch_paths.copilot_token_dir
    )
    set_copilot_usage_reader(
        lambda name: orchestrator._copilot.usage(name) if orchestrator._copilot else None
    )
    register_orchestrator_rpc(app, orchestrator)
    app.state.x11 = orchestrator._x11
    register_x11(app, app.state.x11)
    app.state.reauth = ReauthManager(
        timeout_s=get_settings().reauth_timeout_s,
        x11=lambda: app.state.x11,
    )
    register_reauth(app, app.state.reauth)
    from server.mcp_auth import build_default_manager as _build_mcp_auth
    app.state.mcp_auth = _build_mcp_auth()
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
    except Exception as exc:
        log.error("daemon rehydrate() failed — starting with no sessions restored", exc_info=True)
        # Feed the startup report (surfaced via /api/daemon/health on the edge) so a
        # boot that silently restored nothing is observable, not just a log line.
        orchestrator.note_startup_failure("rehydrate", exc)
    try:
        await orchestrator.rearm_background_tasks()
    except Exception as exc:
        log.error("daemon rearm_background_tasks() failed", exc_info=True)
        orchestrator.note_startup_failure("rearm", exc)
    await orchestrator.start()
    # Drain in-flight turns on SIGTERM before uvicorn begins shutting down, whoever
    # sent it (deploy, watchdog, a bare `systemctl restart`). Needs KillMode=mixed and
    # a TimeoutStopSec that covers the drain caps — see the unit file.
    uninstall_drain_on_sigterm = install_drain_on_sigterm(orchestrator)
    # RC1 (blocking sqlite3 on the loop) ran for hours before it was noticed, and only
    # via an external py-spy dump — this makes the same class of saturation
    # self-reporting instead of requiring an audit. Deliberately unguarded: a watchdog
    # whose own wiring silently fails to start reproduces the exact failure mode it
    # exists to catch. `app.state` is guaranteed by build_daemon(); a broken import or
    # a bad refactor here should crash startup loudly, the same as orchestrator.start()
    # above, so deploy/health checks catch it.
    from server.obs.loop_lag import LoopLagWatchdog
    from server.obs.tasks import supervise
    from server.config import env_float
    app.state.loop_lag = LoopLagWatchdog(
        # Opt-in until the ThreadStallTracer proves itself under load (#792): the
        # old faulthandler dumper segfaulted the daemon and had to be disabled in
        # prod. Set CCSWITCH_LOOP_STALL_DUMP_S to a positive value to enable.
        dump_after=env_float("CCSWITCH_LOOP_STALL_DUMP_S", 0.0, minimum=0.0),
        dump_path=os.environ.get("CCSWITCH_LOOP_STALL_DUMP_PATH") or None,
    )
    app.state.loop_lag_task = supervise(
        asyncio.create_task(app.state.loop_lag.run(), name="loop-lag-watchdog"),
        name="loop-lag-watchdog",
    )
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
        app.state.reauth.sweep_stale()
    except Exception:
        log.warning("daemon startup: reauth sweep_stale failed", exc_info=True)
    try:
        yield
    finally:
        uninstall_drain_on_sigterm()
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
        try:
            await app.state.reauth.shutdown()
        except Exception:
            log.warning("daemon shutdown: reauth shutdown failed", exc_info=True)
        try:
            loop_lag_task = getattr(app.state, "loop_lag_task", None)
        except Exception:
            loop_lag_task = None
        if loop_lag_task is not None:
            loop_lag_task.cancel()
            try:
                await loop_lag_task
            except asyncio.CancelledError:
                pass
        await orchestrator.stop()


def serve(socket_path: str | None = None) -> None:
    # Back-compat: an explicit socket_path arg forces a unix bind at that path.
    endpoint = Endpoint("unix", path=socket_path) if socket_path else sessiond_endpoint()
    app, orchestrator = build_daemon()
    app.router.lifespan_context = lambda _app: daemon_lifespan(_app, orchestrator)
    where = endpoint.path if endpoint.is_unix else f"{endpoint.host}:{endpoint.port} (mTLS)"
    log.info("session daemon serving on %s", where)
    uvicorn.run(
        app,
        log_level="info",
        timeout_graceful_shutdown=_DAEMON_GRACEFUL_SHUTDOWN_TIMEOUT_S,
        **uvicorn_run_kwargs(endpoint, allowlist=client_allowlist()),
    )
