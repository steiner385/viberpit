"""Production entrypoint plumbing for the session daemon: build the app with a real
orchestrator, run its lifecycle, and serve it on a Unix socket or mTLS TCP port."""
import asyncio
import logging
import os
import ssl
from contextlib import asynccontextmanager

import uvicorn

from server.aio import cancel_and_reap
from server.config import env_float, get_settings
from server.db import init_db
from server.http_keepalive import SERVER_KEEPALIVE_TIMEOUT_S
from server.ipc.daemon import create_daemon_app
from server.ipc.drain_on_signal import install_drain_on_sigterm
from server.ipc.orchestrator_rpc import register_orchestrator_rpc
from server.ipc.creds_rpc import register_creds
from server.ipc.reauth_rpc import register_reauth
from server.ipc.x11_rpc import register_x11
from server.obs.jsonlog import configure_logging
from server.reauth import ReauthManager
from server.ipc.paths import Endpoint, sessiond_endpoint
from server.ipc.tls import DaemonTLSFiles, daemon_tls_files, daemon_ssl_context_factory, client_allowlist
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.x11manager import build_x11_manager
from viberpit import credential_envelope, fleet_role

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

# How long the boot resume pass waits for the edge to renew a lapsed credential lease: two
# push ticks, so one missed push does not send every resumed session out tokenless.
BOOT_CREDS_WAIT_S = env_float("VIBERPIT_BOOT_CREDS_WAIT_S",
                              2 * credential_envelope.CREDS_PUSH_INTERVAL_S, minimum=0.0)


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
    from server.sessions.native_display import build_native_display
    orchestrator._native = build_native_display(get_settings())
    from server.sessions.copilot_proxy import build_copilot_proxy_manager
    from server.sessions.copilot_auth import CopilotAuthManager
    from viberpit import paths as _ccswitch_paths
    orchestrator._copilot = build_copilot_proxy_manager(get_settings())
    orchestrator._copilot_auth = CopilotAuthManager(
        token_dir_for=_ccswitch_paths.copilot_token_dir
    )
    # git clone/fetch authenticate with a per-owner App installation token, and repo
    # enumeration lists the App's installation repos, rather than the machine's ambient
    # gh user (issues #887, #912). Registered HERE because the daemon is the process
    # where gitrepos.ensure_clone runs; the edge and deploy controller register the same
    # seams from their own entrypoints. No-ops to ambient/viewer when no App is configured.
    from server.github import app_auth as _app_auth
    _app_auth.register_git_providers()
    register_orchestrator_rpc(app, orchestrator)
    # `/info` reads the live orchestrator for its capacity and engine advertisement.
    # Nothing set this before, so `activeSessions` was hard-wired to 0 on every
    # daemon and the ranker's headroom term was dead weight — and there was no
    # handle through which to publish `engines` at all.
    app.state.orchestrator = orchestrator
    app.state.x11 = orchestrator._x11
    register_x11(app, app.state.x11, orchestrator=orchestrator)
    app.state.reauth = ReauthManager(
        timeout_s=get_settings().reauth_timeout_s,
        x11=lambda: app.state.x11,
    )
    register_reauth(app, app.state.reauth)
    register_creds(app)
    from server.mcp_auth import build_default_manager as _build_mcp_auth
    app.state.mcp_auth = _build_mcp_auth()
    return app, orchestrator


async def _await_creds_lease(timeout_s: float, poll_s: float) -> None:
    """On a broker worker, wait (bounded) for the edge to renew the envelope lease.

    The cached lease (``CREDS_LEASE_S``) never survives a deploy restart — the drain alone can
    outlast it — and a lapsed lease makes ``oauth_env_for`` fail closed, so a session resumed
    now launches with no token and 401s into an auth-recovery relaunch. The edge's
    ``creds_sync`` lands within one push tick of the socket binding. Past *timeout_s* the pass
    proceeds anyway: the auth-recovery ladder still backstops a missed push.
    """
    if not fleet_role.is_worker():
        return
    store = credential_envelope.runner_store()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while store.awaiting_renewal():
        if loop.time() >= deadline:
            log.warning("daemon boot: credential lease still lapsed after %.0fs — "
                        "resuming sessions without waiting further", timeout_s)
            return
        await asyncio.sleep(poll_s)


async def _boot_resume_pass(orchestrator, *, creds_wait_s: float = BOOT_CREDS_WAIT_S,
                            creds_poll_s: float = 1.0) -> None:
    """Re-arm, then auto-resume (auto-resume skips what re-arm already replayed)."""
    await _await_creds_lease(creds_wait_s, creds_poll_s)
    try:
        await orchestrator.rearm_background_tasks()
    except Exception as exc:
        log.error("daemon rearm_background_tasks() failed", exc_info=True)
        orchestrator.note_startup_failure("rearm", exc)
    try:
        await orchestrator.auto_resume_interrupted()
    except Exception:
        log.error("daemon auto_resume_interrupted() failed", exc_info=True)


@asynccontextmanager
async def daemon_lifespan(app, orchestrator):
    """Daemon startup/shutdown: init the shared DB, start the orchestrator (poller +
    rotation + global pump), and stop it cleanly. Rehydrate is lazy — no driver spawned
    at startup, so a daemon start does not respawn every session at once."""
    # The daemon owns every live `claude` process, so it is where the SDK's stream
    # generators are abandoned and GC-finalized. Installed first, and from INSIDE
    # the running loop (run_forever replaces anything set before it), so the
    # unattributed "Task exception was never retrieved" those closes produce is
    # named instead of filed as a fresh mystery (#1869, #1870).
    from server.obs.asyncgen_finalize import install_asyncgen_finalize_reporter
    install_asyncgen_finalize_reporter()

    db_url = os.environ.get("VIBERPIT_DB_URL", "sqlite+aiosqlite:///:memory:")
    await init_db(db_url=db_url)

    from server.obs.errsink import get_error_sink
    error_sink = get_error_sink()
    if error_sink is not None:
        error_sink.start()

    # Wire the Codex app-server manager BEFORE anything can spawn a session
    # (rehydrate/auto_resume below both can). It is built here rather than in
    # build_daemon() because deciding availability probes the real binary, which is
    # async I/O. It stays None when Codex is disabled, unsupported or absent — the
    # orchestrator's two Codex hooks are then no-ops and nothing else changes. The
    # guard is deliberate: no Codex probe may prevent a daemon that serves Claude
    # sessions from starting.
    from server.codex.manager import build_codex_manager
    try:
        codex_manager = await build_codex_manager(get_settings())
    except Exception:
        log.warning("daemon startup: codex manager build failed — codex disabled",
                    exc_info=True)
        codex_manager = None
    orchestrator.install_codex_manager(codex_manager)
    if orchestrator._codex is not None:
        try:
            reaped = orchestrator._codex.sweep_orphans()
            if reaped:
                log.info("daemon startup: reaped %d orphaned codex app-server(s)", reaped)
        except Exception:
            log.warning("daemon startup: codex orphan reap failed", exc_info=True)
    # One structured startup verdict (`codex.doctor.report`) for the six independent
    # ways a Codex runner can be misconfigured — the binary, the runtime home, the
    # authority, the client certificate, the account manifest, and the launch flags.
    # It REUSES the availability the manager just computed rather than probing the
    # binary a second time, and it runs as a supervised background task because one
    # of its checks talks to the authority under a 20 s admin deadline: a daemon that
    # owns live `claude` processes may not have its boot delayed by diagnostics.
    from server.codex.doctor import run_and_log as _codex_doctor
    from server.obs.tasks import supervise as _supervise
    app.state.codex_doctor_task = _supervise(
        asyncio.create_task(
            _codex_doctor(
                get_settings(),
                availability=getattr(orchestrator._codex, "availability", None),
            ),
            name="codex-doctor",
        ),
        name="codex-doctor",
    )
    # Reap `claude` CLIs the PREVIOUS incarnation left running (#1379) — before
    # anything below can rehydrate or resume. KillMode=mixed lets the old unit's
    # children outlive it ("Unit process … (claude) remains running after unit
    # stopped" on every deploy); a survivor keeps editing files and can push, while
    # the freshly-resumed session has no idea it exists. Same placement and same
    # never-fatal contract as the Codex and X11 orphan sweeps around it.
    try:
        reaped = await orchestrator.reap_orphan_clis()
        if reaped:
            log.warning("daemon startup: reaped %d orphaned claude CLI(s) left by a "
                        "previous daemon", reaped)
    except Exception:
        log.warning("daemon startup: orphan CLI reap failed", exc_info=True)
    try:
        await orchestrator.rehydrate()
    except Exception as exc:
        log.error("daemon rehydrate() failed — starting with no sessions restored", exc_info=True)
        # Feed the startup report (surfaced via /api/daemon/health on the edge) so a
        # boot that silently restored nothing is observable, not just a log line.
        orchestrator.note_startup_failure("rehydrate", exc)
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
    from server.obs.loop_lag import watchdog_from_env
    from server.obs.tasks import supervise
    # Stack dump on a >5s stall is on by default (#1913); VIBERPIT_LOOP_STALL_DUMP_S=0
    # turns it off. See server.obs.loop_lag.watchdog_from_env.
    app.state.loop_lag = watchdog_from_env()
    app.state.loop_lag_task = supervise(
        asyncio.create_task(app.state.loop_lag.run(), name="loop-lag-watchdog"),
        name="loop-lag-watchdog",
    )
    # Per-session memory attribution (#2739): names the session whose process subtree
    # is ballooning BEFORE the kernel's OOM line, which only carries a bare pid.
    from server.sessions.memory_watch import SessionKey, memory_watch_from_env
    memory_watch = memory_watch_from_env(lambda: [
        SessionKey(r.id, r.engine_session_id, r.cwd) for r in orchestrator.list_sessions()])
    app.state.memory_watch_task = None if memory_watch is None else supervise(
        asyncio.create_task(memory_watch.run(), name="session-memory-watch"),
        name="session-memory-watch",
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
    # Re-arm drain-interrupted sessions, then auto-resume, BEHIND the yield: uvicorn
    # binds its socket only once this lifespan yields, and each re-arm resumes a
    # session under the orchestrator's global `_ensure_lock` (~1 min apiece). Awaited
    # inline, five interrupted sessions held rocky off :9443 for 6.5 min and failed
    # the deploy's 180 s verify. Placed after install_drain_on_sigterm so a stop that
    # lands mid-pass still drains the turns it started; a cancelled pass keeps its
    # unsent snapshots for the next boot or attach (`_maybe_rearm_on_attach`).
    app.state.boot_resume_task = supervise(
        asyncio.create_task(_boot_resume_pass(orchestrator), name="daemon-boot-resume"),
        name="daemon-boot-resume",
    )
    try:
        yield
    finally:
        uninstall_drain_on_sigterm()
        await cancel_and_reap(app.state.boot_resume_task, phase="daemon.boot-resume")
        # Drain the Codex app-servers first. By the time this runs, install_drain_on_sigterm
        # has already let in-flight turns settle, so no thread is mid-turn — which is the
        # ordering the plan requires ("after driver turns settle and before daemon shutdown
        # completes"), and the reason it is not safe to stop these any earlier: killing an
        # app-server under a live turn is exactly the crash the manager has to treat as
        # indeterminate. stop_all() tears them down CONCURRENTLY under the same wait_for cap
        # as the x11 sweep, so the whole shutdown stays inside systemd's TimeoutStopSec.
        codex = getattr(orchestrator, "_codex", None)
        if codex is not None:
            try:
                await asyncio.wait_for(
                    codex.stop_all(), timeout=_DAEMON_SHUTDOWN_SWEEP_TIMEOUT_S)
            except Exception:
                log.warning("daemon shutdown: codex teardown failed", exc_info=True)
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
        native = getattr(orchestrator, "_native", None)
        if native is not None:
            try:
                await asyncio.wait_for(
                    native.stop_all(), timeout=_DAEMON_SHUTDOWN_SWEEP_TIMEOUT_S)
            except Exception:
                log.warning("daemon shutdown: native display teardown failed", exc_info=True)
        try:
            await app.state.reauth.shutdown()
        except Exception:
            log.warning("daemon shutdown: reauth shutdown failed", exc_info=True)
        try:
            await app.state.mcp_auth.shutdown()
        except Exception:
            log.warning("daemon shutdown: mcp_auth shutdown failed", exc_info=True)
        try:
            loop_lag_task = getattr(app.state, "loop_lag_task", None)
        except Exception:
            loop_lag_task = None
        if loop_lag_task is not None:
            await cancel_and_reap(loop_lag_task, phase="daemon.loop-lag")
        memory_watch_task = getattr(app.state, "memory_watch_task", None)
        if memory_watch_task is not None:
            await cancel_and_reap(memory_watch_task, phase="daemon.memory-watch")
        if error_sink is not None:
            try:
                await asyncio.wait_for(error_sink.stop(), timeout=_DAEMON_SHUTDOWN_SWEEP_TIMEOUT_S)
            except Exception:  # noqa: BLE001 — never let the sink block daemon shutdown
                log.warning("daemon shutdown: error sink stop failed", exc_info=True)
        # A daemon stopped within seconds of boot can still have the doctor waiting
        # on the authority's 20 s admin deadline; reap it the same way, never with a
        # hand-rolled cancel (#1164).
        doctor_task = getattr(app.state, "codex_doctor_task", None)
        if doctor_task is not None:
            await cancel_and_reap(doctor_task, phase="daemon.codex-doctor")
        await orchestrator.stop()


def serve(socket_path: str | None = None) -> None:
    # #1423: the daemon owns the orchestrator in remote mode, so it is most of the
    # app's diagnostic surface — and it never configured logging, so every INFO
    # was dropped and WARNING+ leaked as bare text. Same call the edge makes.
    configure_logging(process="daemon")
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
        # Must outlive the edge pool's keepalive_expiry — see server/http_keepalive.py.
        timeout_keep_alive=SERVER_KEEPALIVE_TIMEOUT_S,
        **uvicorn_run_kwargs(endpoint, allowlist=client_allowlist()),
    )
