from contextlib import asynccontextmanager
import asyncio
import logging
import os
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import select as _sa_select
import server.db as _db
from server.db.models import Session as _SessionModel
from server.spa import mount_spa

from server.obs.jsonlog import configure_logging
from server.obs.middleware import RequestContextMiddleware
from server.auth.middleware import IdentityMiddleware, current_identity
from server.bridge.ws import build_ws_router
from server.db import init_db
from server.ipc.client import DaemonUnavailable, DaemonError
from server.reauth import ReauthManager
from server.sessions.orchestrator import SessionOrchestrator
from server.version import running_git_sha

# API routers (T036–T038)
from server.api.sessions import router as sessions_router
from server.api.attachments import router as attachments_router
from server.api.accounts import router as accounts_router
from server.api.auth import router as auth_router
from server.api.config import router as config_router
from server.api.repos import router as github_router
from server.api.settings import router as settings_router
from server.api import models as api_models
from server.api.deploy import router as deploy_router
from server.api.client_logs import router as client_logs_router
from server.api.push import router as push_router
from server.api.pipeline import router as pipeline_router, repos_router as pipeline_repos_router
from server.api.automations import router as automations_router
from server.api.library import router as library_router


def _daemon_present(endpoint) -> bool:
    """True if something is listening at the endpoint (socket file exists / TCP connects)."""
    if isinstance(endpoint, str):
        from server.ipc.paths import parse_endpoint
        endpoint = parse_endpoint(endpoint if "://" in endpoint else f"unix://{endpoint}")
    if endpoint.is_unix:
        import os
        import stat
        try:
            return stat.S_ISSOCK(os.stat(endpoint.path).st_mode)
        except OSError:
            return False
    import socket
    try:
        with socket.create_connection((endpoint.host, endpoint.port), timeout=1.0):
            return True
    except OSError:
        return False


def _daemon_compatible(endpoint, *, attempts: int = 5, interval: float = 0.5) -> bool:
    """True iff a protocol-compatible daemon answers /info at the endpoint. Synchronous."""
    if isinstance(endpoint, str):
        from server.ipc.paths import parse_endpoint
        endpoint = parse_endpoint(endpoint if "://" in endpoint else f"unix://{endpoint}")
    import time

    import httpx

    from server.ipc.protocol import PROTOCOL_VERSION
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        logging.getLogger("server").warning(
            "skipping synchronous daemon probe at %s: create_app() ran under a live "
            "event loop where blocking I/O would freeze (or self-deadlock) it", endpoint)
        return False
    if not _daemon_present(endpoint):
        return False

    def _client() -> httpx.Client:
        if endpoint.is_unix:
            return httpx.Client(transport=httpx.HTTPTransport(uds=endpoint.path),
                                base_url="http://daemon", timeout=1.0)
        from server.ipc.tls import client_ssl_context, client_tls_files
        ctx = client_ssl_context(client_tls_files())
        return httpx.Client(base_url=f"https://{endpoint.host}:{endpoint.port}",
                            verify=ctx, timeout=1.0)

    for i in range(max(1, attempts)):
        try:
            with _client() as c:
                info = c.get("/info").json()
            if info.get("protocol") == PROTOCOL_VERSION:
                return True
        except Exception:  # noqa: BLE001 — unreachable / bad response → retry
            pass
        if i < attempts - 1:
            time.sleep(interval)
    return False


# Shutdown cleanup budget — must stay comfortably under systemd TimeoutStopSec (60s)
# so graceful display teardown completes instead of being SIGKILLed (issue #206).
_SHUTDOWN_CLOSE_TIMEOUT_S = 30.0
_SHUTDOWN_SWEEP_TIMEOUT_S = 15.0


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """App startup/shutdown: init DB, install log filter, start orchestrator."""
    # Structured JSON logging (stdout→journald + rotating file) with correlation
    # ids and credential redaction. Idempotent; installs the redacting filter too.
    configure_logging()

    # An app not built by create_app (e.g. tests constructing FastAPI(lifespan=_lifespan)
    # directly) never sets app.state.remote_mode — default it to in-process. The local
    # persists across the yield, so the shutdown branch reads it too.
    remote_mode = getattr(app.state, "remote_mode", False)

    # F1: the identity header is only trustworthy behind `tailscale serve` on a
    # loopback bind.  Hard-fail if neither an allowlist nor a loopback host is set
    # (unless the operator explicitly opts out with CCSWITCH_DEV=1).
    import logging as _logging

    from server.config import get_settings as _get_settings

    _s = _get_settings()
    _is_loopback = _s.host in ("127.0.0.1", "localhost", "::1")
    _is_dev = os.environ.get("CCSWITCH_DEV") == "1"

    if not _s.tailnet_allowlist_items and not _is_loopback:
        if not _is_dev:
            raise RuntimeError(
                "refusing to start: empty tailnet_allowlist on a non-loopback bind "
                "(set CCSWITCH_DEV=1 to override, or set CCSWITCH_TAILNET_ALLOWLIST)."
            )
        _logging.getLogger("server").warning(
            "SECURITY: binding to %s with an EMPTY tailnet allowlist. The "
            "Tailscale-User-Login header is trusted unconditionally — only safe "
            "behind `tailscale serve` (which sets/strips it). Set "
            "CCSWITCH_TAILNET_ALLOWLIST and/or bind 127.0.0.1.",
            _s.host,
        )

    db_url = os.environ.get("CCSWITCH_DB_URL", "sqlite+aiosqlite:///:memory:")
    await init_db(db_url=db_url)

    from server.automations.registry import registry as _automation_registry
    try:
        await _automation_registry.ensure_rows()
    except Exception:
        _logging.getLogger("server").warning(
            "startup: automation ensure_rows failed", exc_info=True
        )

    from server.db import db_session as _db_session_factory
    from server.usage.auth_failure_audit import AuthFailureAuditor
    _auth_auditor = AuthFailureAuditor(identity="system", session_factory=_db_session_factory)
    _auth_auditor.start()
    app.state.auth_failure_auditor = _auth_auditor

    # Rehydrate persisted sessions (lazy — drivers are created on attach, not here).
    # In remote mode the daemon owns the orchestrator lifecycle, so skip it here.
    if not remote_mode:
        try:
            await app.state.orchestrator.rehydrate()
        except Exception:
            _logging.getLogger("server").error(
                "rehydrate() failed — starting with no sessions restored", exc_info=True
            )

    # Sweep stale reauth-tmp dirs (from crashed previous runs)
    app.state.reauth.sweep_stale()

    # NOTE: do NOT install SIGTERM/SIGINT handlers here. uvicorn installs its own
    # handle_exit signal handlers (via loop.add_signal_handler) BEFORE it runs this
    # lifespan startup; registering ours would overwrite them, so SIGTERM would no
    # longer set uvicorn's should_exit and the server never begins graceful shutdown.
    # systemd then SIGKILLs the process at TimeoutStopSec on every deploy restart —
    # and this lifespan's own shutdown branch (session close + X11 sweep) never runs,
    # resurrecting the leaked-display bug (#206). Any running reauth is a background
    # task that gets cancelled by app.state.reauth.shutdown() in the shutdown branch
    # below, so no early-cancel signal handler is needed.

    # Start the singleton orchestrator (usage poller + rotation monitor).
    # In remote mode the daemon runs these, not the edge.
    if not remote_mode:
        await app.state.orchestrator.start()

    # Daemon-health monitor (issue #434): in remote mode the edge polls the daemon's /info
    # epoch to make wedge→watchdog-restart events visible in the dashboard. In-process mode
    # has no separate daemon, so there's nothing to watch.
    app.state.daemon_health = None
    if remote_mode:
        from server.ipc.client import DaemonClient
        from server.sessions.daemon_health import DaemonHealthMonitor
        _dh_client = DaemonClient(app.state.daemon_endpoint)
        app.state._daemon_health_client = _dh_client
        monitor = DaemonHealthMonitor(info_fn=_dh_client.info)
        monitor.start()
        app.state.daemon_health = monitor

    # Sweep stale X lock/socket files AND reap orphaned Xvfb/x11vnc/chrome left by a
    # prior SIGKILLed shutdown (issue #206 — the self-healing startup half).
    if getattr(app.state, "x11", None) is not None:
        try:
            n = app.state.x11.sweep_stale()
            if n:
                _logging.getLogger("server").info("startup: swept %d stale X lock/socket(s)", n)
        except Exception:
            _logging.getLogger("server").warning("startup: X sweep failed", exc_info=True)
        try:
            k = app.state.x11.sweep_orphan_processes()
            if k:
                _logging.getLogger("server").info(
                    "startup: reaped %d orphaned X display process(es)", k
                )
        except Exception:
            _logging.getLogger("server").warning("startup: X orphan reap failed", exc_info=True)

    # Sweep stale unbound attachments (uploaded but never sent) from prior runs.
    from server.db import db_session as _db_session
    from server.sessions import attachments as _attachments
    try:
        async with _db_session() as _db:
            n = await _attachments.sweep_orphans(_db)
        if n:
            _logging.getLogger("server").info(
                "startup: swept %d orphan attachment(s)", n
            )
    except Exception:
        _logging.getLogger("server").warning(
            "startup: attachment orphan sweep failed", exc_info=True
        )

    yield
    # Shutdown
    import contextlib
    with contextlib.suppress(Exception):
        app.state.auth_failure_auditor.stop()
    if getattr(app.state, "daemon_health", None) is not None:
        with contextlib.suppress(Exception):
            await app.state.daemon_health.stop()
        with contextlib.suppress(Exception):
            await app.state._daemon_health_client.aclose()
    await app.state.reauth.shutdown()
    if remote_mode:
        # Remote mode: the daemon owns sessions/drivers; just close the IPC client.
        await app.state.sessions.aclose()
    else:
        # Close live sessions before stopping orchestrator — triggers stop_display for each.
        if getattr(app.state, "x11", None) is not None:
            log = _logging.getLogger("server")

            async def _close(sid: str) -> None:
                try:
                    await app.state.orchestrator.close_session(sid)
                except Exception:
                    log.warning("shutdown: close %s failed", sid, exc_info=True)

            sids = list(app.state.orchestrator._drivers.keys())
            if sids:
                # Close CONCURRENTLY and bound the total time. Sequential close of N
                # sessions (~10s each: two 5s terminate timeouts + browser tree kill)
                # exceeded systemd TimeoutStopSec → SIGKILL before cleanup → leaked
                # Xvfb/x11vnc/chrome (issue #206). Concurrency + a hard cap keep
                # graceful teardown well inside the timeout.
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*(_close(sid) for sid in sids)),
                        timeout=_SHUTDOWN_CLOSE_TIMEOUT_S,
                    )
                except asyncio.TimeoutError:
                    log.warning(
                        "shutdown: session close exceeded %ss; sweeping displays",
                        _SHUTDOWN_CLOSE_TIMEOUT_S,
                    )
            # Final sweep: terminate any display still tracked, so a hung close above
            # can never leave Xvfb/x11vnc/chrome behind (defense-in-depth).
            try:
                await asyncio.wait_for(
                    app.state.x11.stop_all(), timeout=_SHUTDOWN_SWEEP_TIMEOUT_S
                )
            except Exception:
                log.warning("shutdown: x11 sweep incomplete", exc_info=True)

        await app.state.orchestrator.stop()

    # Dispose the DB engine LAST — after every DB user (auditor, orchestrator,
    # sessions) has stopped — while this loop is still alive, so the pooled
    # aiosqlite connection's worker thread is joined cleanly instead of leaking a
    # connection bound to a soon-to-close loop (the "Event loop is closed" flood).
    from server.db import dispose_db
    with contextlib.suppress(Exception):
        await dispose_db()


def create_app(
    orchestrator: SessionOrchestrator | None = None,
    reauth_manager: ReauthManager | None = None,
) -> FastAPI:
    app = FastAPI(title="ccswitch", version="0.1.0", lifespan=_lifespan)

    # Attach the session facade. CCSWITCH_SESSIOND_ENDPOINT (https/unix URL) or the
    # legacy CCSWITCH_SESSIOND_SOCKET selects remote mode: the edge drives an
    # orchestrator living in the session daemon over TCP+mTLS or a Unix socket.
    # An explicitly injected orchestrator always wins (in-process), so tests that
    # pass one are unaffected.
    from server.sessions.facade import InProcessOrchestratorAdapter

    from server.ipc.paths import DEFAULT_SOCKET_PATH, Endpoint, sessiond_endpoint

    explicit = bool(os.environ.get("CCSWITCH_SESSIOND_ENDPOINT")
                    or os.environ.get("CCSWITCH_SESSIOND_SOCKET"))
    endpoint = None
    app.state.daemon_protocol = None
    if orchestrator is None:
        if explicit:
            endpoint = sessiond_endpoint()
        else:
            default_ep = Endpoint("unix", path=DEFAULT_SOCKET_PATH)
            if _daemon_compatible(default_ep):
                endpoint = default_ep
                from server.ipc.protocol import PROTOCOL_VERSION
                app.state.daemon_protocol = PROTOCOL_VERSION
                logging.getLogger("server").info(
                    "session daemon detected at %s — running in remote mode", DEFAULT_SOCKET_PATH)
            elif _daemon_present(default_ep):
                raise RuntimeError(
                    f"session daemon socket {DEFAULT_SOCKET_PATH} exists but no compatible "
                    "daemon answered /info after retries; refusing in-process fallback to "
                    "avoid a split-brain over the shared database")
            # else: no daemon on this host → in-process (dev/CI default).

    if orchestrator is None and endpoint is not None:
        from server.ipc.client import DaemonClient
        from server.sessions.remote import RemoteOrchestrator
        client_ssl = None
        if endpoint.is_tcp:
            from server.ipc.tls import client_ssl_context, client_tls_files
            client_ssl = client_ssl_context(client_tls_files())
        app.state.remote_mode = True
        app.state.daemon_endpoint = endpoint
        app.state.daemon_socket = endpoint.path if endpoint.is_unix else None
        app.state.daemon_client_ssl = client_ssl
        app.state.orchestrator = None
        app.state.sessions = RemoteOrchestrator(DaemonClient(endpoint))
    else:
        if orchestrator is None:
            orchestrator = SessionOrchestrator()
        app.state.remote_mode = False
        app.state.daemon_endpoint = None
        app.state.daemon_socket = None
        app.state.daemon_client_ssl = None
        app.state.orchestrator = orchestrator
        app.state.sessions = InProcessOrchestratorAdapter(orchestrator)

    # X11 manager — None when feature is disabled or preflight fails (fail-loud warning)
    from server.config import get_settings as _gs
    from server.sessions.x11manager import build_x11_manager
    if getattr(app.state, "remote_mode", False):
        app.state.x11 = None          # daemon owns the displays; edge proxies over IPC
    else:
        app.state.x11 = build_x11_manager(_gs())
        if orchestrator is not None:
            orchestrator._x11 = app.state.x11

    # Copilot proxy manager — None when proxy binary absent or feature disabled.
    if not getattr(app.state, "remote_mode", False) and orchestrator is not None:
        from server.sessions.copilot_proxy import build_copilot_proxy_manager
        from server.sessions.copilot_auth import CopilotAuthManager
        from ccswitch import paths as _ccswitch_paths
        from ccswitch.accounts import set_copilot_usage_reader
        orchestrator._copilot = build_copilot_proxy_manager(_gs())
        orchestrator._copilot_auth = CopilotAuthManager(
            token_dir_for=_ccswitch_paths.copilot_token_dir
        )
        set_copilot_usage_reader(
            lambda name: orchestrator._copilot.usage(name) if orchestrator._copilot else None
        )

    # Attach the reauth facade. Remote mode proxies to the daemon's manager (which owns
    # the X11 displays); in-process wraps a real manager behind the same async facade.
    from server.reauth_remote import InProcessReauthAdapter, RemoteReauthManager
    if getattr(app.state, "remote_mode", False):
        from server.ipc.client import DaemonClient
        app.state.reauth = RemoteReauthManager(DaemonClient(app.state.daemon_endpoint))
    else:
        from server.config import get_settings
        mgr = reauth_manager or ReauthManager(
            timeout_s=get_settings().reauth_timeout_s,
            x11=lambda: app.state.x11,
        )
        app.state.reauth = InProcessReauthAdapter(mgr)

    # Middleware stack (Starlette runs last-added outermost): RequestContextMiddleware
    # wraps IdentityMiddleware wraps the routes, so request_id is set before identity.
    app.add_middleware(IdentityMiddleware)
    app.add_middleware(RequestContextMiddleware)

    # In remote mode an unreachable daemon surfaces as DaemonUnavailable from any
    # facade RPC; map it to 503 so clients see "try again", not a 500. Harmless
    # in-process (never raised there).
    @app.exception_handler(DaemonUnavailable)
    async def _daemon_unavailable_handler(_request, _exc):  # noqa: ANN001
        return JSONResponse(status_code=503, content={"detail": "session daemon unavailable"})

    @app.exception_handler(DaemonError)
    async def _daemon_error_handler(_request, _exc):  # noqa: ANN001
        return JSONResponse(status_code=409, content={"detail": _exc.message})

    @app.exception_handler(Exception)
    async def _unhandled_handler(_request, _exc):  # noqa: ANN001
        # Last-resort: log full traceback (auto-stamped with request_id) then
        # return a generic 500 so internals never leak to the client. FastAPI
        # tries specific handlers first, so domain mappings above still win.
        logging.getLogger("server").exception("unhandled exception: %s", _exc)
        return JSONResponse(status_code=500, content={"detail": "internal server error"})

    @app.get("/api/health")
    async def health(_identity: str = Depends(current_identity)) -> dict:
        return {"ok": True, "version": "0.1.0"}

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        # Exercises every Session model column; a drifted DB raises → 500.
        # Active count comes through the facade (async) so it works in both modes;
        # in remote mode an unreachable daemon degrades gracefully (200 + flag).
        active = 0
        daemon_state = None
        try:
            active = await app.state.sessions.active_session_count()
            if app.state.remote_mode:
                daemon_state = "ok"
        except DaemonUnavailable:
            active = 0
            daemon_state = "unavailable"
        if app.state.remote_mode:
            try:
                m = await app.state.sessions.x11_metrics()
                vnc_attached = bool(m.get("vnc_clients_attached", 0) > 0)
            except (DaemonUnavailable, DaemonError):
                vnc_attached = False
        else:
            x11 = getattr(app.state, "x11", None)
            vnc_attached = bool(x11 and x11.metrics().get("vnc_clients_attached", 0) > 0)
        try:
            async with _db.db_session() as db:
                await db.execute(_sa_select(_SessionModel).limit(1))
            payload = {
                "status": "ok", "sha": running_git_sha(),
                "busy": active > 0, "activeSessions": active,
                "vncAttached": vnc_attached,
            }
            if daemon_state is not None:
                payload["daemon"] = daemon_state
            return JSONResponse(payload)
        except Exception as exc:  # noqa: BLE001 — any DB/schema failure is unhealthy
            logging.getLogger("server").warning("healthz schema check failed", exc_info=exc)
            return JSONResponse({"status": "schema_error", "sha": running_git_sha()}, status_code=500)

    # REST API routers (T036–T038)
    app.include_router(sessions_router)
    app.include_router(attachments_router)
    app.include_router(accounts_router)
    app.include_router(auth_router)
    app.include_router(config_router)
    app.include_router(github_router)
    app.include_router(settings_router)
    app.include_router(api_models.router)
    app.include_router(deploy_router)
    app.include_router(client_logs_router)
    app.include_router(push_router)
    app.include_router(pipeline_router)
    app.include_router(pipeline_repos_router)
    app.include_router(automations_router)
    app.include_router(library_router)

    # WebSocket bridge
    ws_router = build_ws_router(app.state.sessions)
    app.include_router(ws_router)

    # VNC WebSocket bridge — gated by x11 state at call time
    from server.bridge.vnc_ws import build_vnc_ws_router
    app.include_router(build_vnc_ws_router(
        lambda: getattr(app.state, "x11", None),
        lambda: getattr(app.state, "daemon_endpoint", None),
        lambda: getattr(app.state, "daemon_client_ssl", None),
    ))

    @app.get("/api/x11/metrics")
    async def x11_metrics(_identity: str = Depends(current_identity)) -> dict:
        if getattr(app.state, "remote_mode", False):
            try:
                return await app.state.sessions.x11_metrics()
            except DaemonUnavailable:
                return {"enabled": False}
        x11 = getattr(app.state, "x11", None)
        return x11.metrics() if x11 else {"enabled": False}

    @app.get("/api/daemon/health")
    async def daemon_health(_identity: str = Depends(current_identity)) -> dict:
        # Issue #434: surface daemon lifecycle health (restart count / uptime / epoch) so a
        # wedge→watchdog-restart is self-evident. Only meaningful in remote mode — in-process
        # runs have no separate daemon to watch.
        monitor = getattr(app.state, "daemon_health", None)
        if monitor is None:
            return {"available": False, "epoch": None, "startedAt": None,
                    "uptimeSec": None, "restartCount24h": 0, "lastRestartAt": None}
        return monitor.snapshot()

    from fastapi import Body, HTTPException  # noqa: PLC0415 — local import avoids top-level churn

    @app.post("/api/sessions/{session_id}/browser/open")
    async def open_browser(
        session_id: str,
        payload: dict = Body(default={}),
        _identity: str = Depends(current_identity),
    ) -> dict:
        if getattr(app.state, "remote_mode", False):
            res = await app.state.sessions.x11_open_browser(session_id, payload.get("url"))
            if not res.get("ok"):
                detail = "no display for session" if res.get("reason") == "no_display" else "browser cap reached"
                raise HTTPException(status_code=409, detail=detail)
            return {"ok": True}
        x11 = getattr(app.state, "x11", None)
        if x11 is None or x11.display_for(session_id) is None:
            raise HTTPException(status_code=409, detail="no display for session")
        ok = await x11.launch_browser(session_id, payload.get("url"))
        if not ok:
            raise HTTPException(status_code=409, detail="browser cap reached")
        return {"ok": True}

    # SPA static files with client-side-routing fallback — optional; guarded so a
    # missing build (dev/test) doesn't break startup. Registered AFTER the API/WS
    # routers so /api/* and /ws/* keep precedence.
    mount_spa(app, Path(__file__).parent.parent / "web" / "dist")

    return app


app = create_app()
