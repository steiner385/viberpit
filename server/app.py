from contextlib import asynccontextmanager
import asyncio
import functools
import logging
import os
import signal
import subprocess
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import select as _sa_select
import server.db as _db
from server.db.models import Session as _SessionModel
from server.spa import mount_spa

from server.audit import install_redacting_filter
from server.auth.middleware import IdentityMiddleware, current_identity
from server.bridge.ws import build_ws_router
from server.db import init_db
from server.reauth import ReauthManager
from server.sessions.orchestrator import SessionOrchestrator

# API routers (T036–T038)
from server.api.sessions import router as sessions_router
from server.api.attachments import router as attachments_router
from server.api.accounts import router as accounts_router
from server.api.auth import router as auth_router
from server.api.config import router as config_router
from server.api.repos import router as github_router
from server.api.settings import router as settings_router


@functools.lru_cache(maxsize=1)
def running_git_sha() -> str:
    """Short git SHA of the running code, for deploy verification via /healthz.

    Resolved once: ``CCSWITCH_GIT_SHA`` (the deployer can inject it) wins; else
    ``git rev-parse --short HEAD`` against this checkout; else ``"unknown"`` (e.g.
    a source tarball with no .git). Lets a deploy assert THIS commit is live, not
    just that some process answered.
    """
    env_sha = os.environ.get("CCSWITCH_GIT_SHA")
    if env_sha:
        return env_sha.strip()
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True, text=True, timeout=5, check=True,
        )
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """App startup/shutdown: init DB, install log filter, start orchestrator."""
    install_redacting_filter()

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

    # Rehydrate persisted sessions (lazy — drivers are created on attach, not here).
    # In remote mode the daemon owns the orchestrator lifecycle, so skip it here.
    if not app.state.remote_mode:
        try:
            await app.state.orchestrator.rehydrate()
        except Exception:
            _logging.getLogger("server").error(
                "rehydrate() failed — starting with no sessions restored", exc_info=True
            )

    # Sweep stale reauth-tmp dirs (from crashed previous runs)
    app.state.reauth.sweep_stale()

    # Register SIGTERM/SIGINT handlers to cleanly cancel any running reauth
    loop = asyncio.get_running_loop()

    def _handle_signal(sig):
        loop.create_task(app.state.reauth.shutdown())

    try:
        loop.add_signal_handler(signal.SIGTERM, _handle_signal, signal.SIGTERM)
        loop.add_signal_handler(signal.SIGINT, _handle_signal, signal.SIGINT)
    except (NotImplementedError, RuntimeError):
        # Signal handlers are not available on Windows or in some test contexts
        pass

    # Start the singleton orchestrator (usage poller + rotation monitor).
    # In remote mode the daemon runs these, not the edge.
    if not app.state.remote_mode:
        await app.state.orchestrator.start()

    # Sweep stale X lock/socket files from crashed previous runs
    if getattr(app.state, "x11", None) is not None:
        try:
            n = app.state.x11.sweep_stale()
            if n:
                _logging.getLogger("server").info("startup: swept %d stale X lock/socket(s)", n)
        except Exception:
            _logging.getLogger("server").warning("startup: X sweep failed", exc_info=True)

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
    await app.state.reauth.shutdown()
    if app.state.remote_mode:
        # Remote mode: the daemon owns sessions/drivers; just close the IPC client.
        await app.state.sessions.aclose()
    else:
        # Close live sessions before stopping orchestrator — triggers stop_display for each
        if getattr(app.state, "x11", None) is not None:
            for sid in list(app.state.orchestrator._drivers.keys()):
                try:
                    await app.state.orchestrator.close_session(sid)
                except Exception:
                    _logging.getLogger("server").warning(
                        "shutdown: close %s failed", sid, exc_info=True
                    )

        await app.state.orchestrator.stop()


def create_app(
    orchestrator: SessionOrchestrator | None = None,
    reauth_manager: ReauthManager | None = None,
) -> FastAPI:
    app = FastAPI(title="ccswitch", version="0.1.0", lifespan=_lifespan)

    # Attach the session facade. CCSWITCH_SESSIOND_SOCKET selects remote mode:
    # the edge drives an orchestrator living in the session daemon over a Unix
    # socket. An explicitly injected orchestrator always wins (in-process), so
    # tests that pass one are unaffected.
    from server.sessions.facade import InProcessOrchestratorAdapter

    socket_path = os.environ.get("CCSWITCH_SESSIOND_SOCKET")
    if orchestrator is None and socket_path:
        from server.ipc.client import DaemonClient
        from server.sessions.remote import RemoteOrchestrator
        app.state.remote_mode = True
        app.state.orchestrator = None
        app.state.sessions = RemoteOrchestrator(DaemonClient(socket_path))
    else:
        if orchestrator is None:
            orchestrator = SessionOrchestrator()
        app.state.remote_mode = False
        app.state.orchestrator = orchestrator
        app.state.sessions = InProcessOrchestratorAdapter(orchestrator)

    # X11 manager — None when feature is disabled or preflight fails (fail-loud warning)
    from server.config import get_settings as _gs
    from server.sessions.x11manager import build_x11_manager
    app.state.x11 = build_x11_manager(_gs())
    orchestrator._x11 = app.state.x11  # share the manager with the orchestrator

    # Attach the ReauthManager (allows tests to inject a fake)
    if reauth_manager is None:
        from server.config import get_settings
        reauth_manager = ReauthManager(timeout_s=get_settings().reauth_timeout_s)
    app.state.reauth = reauth_manager

    # Identity middleware — wraps all routes
    app.add_middleware(IdentityMiddleware)

    @app.get("/api/health")
    async def health(_identity: str = Depends(current_identity)) -> dict:
        return {"ok": True, "version": "0.1.0"}

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        # Exercises every Session model column; a drifted DB raises → 500.
        # Active count comes through the facade (async) so it works in both modes;
        # in remote mode an unreachable daemon degrades gracefully (200 + flag).
        from server.ipc.client import DaemonUnavailable

        active = 0
        daemon_state = None
        try:
            active = await app.state.sessions.active_session_count()
            if app.state.remote_mode:
                daemon_state = "ok"
        except DaemonUnavailable:
            active = 0
            daemon_state = "unavailable"
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

    # WebSocket bridge
    ws_router = build_ws_router(app.state.sessions)
    app.include_router(ws_router)

    # VNC WebSocket bridge — gated by x11 state at call time
    from server.bridge.vnc_ws import build_vnc_ws_router
    app.include_router(build_vnc_ws_router(lambda: getattr(app.state, "x11", None)))

    @app.get("/api/x11/metrics")
    async def x11_metrics(_identity: str = Depends(current_identity)) -> dict:
        x11 = getattr(app.state, "x11", None)
        return x11.metrics() if x11 else {"enabled": False}

    from fastapi import Body, HTTPException  # noqa: PLC0415 — local import avoids top-level churn

    @app.post("/api/sessions/{session_id}/browser/open")
    async def open_browser(
        session_id: str,
        payload: dict = Body(default={}),
        _identity: str = Depends(current_identity),
    ) -> dict:
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
