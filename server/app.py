from contextlib import asynccontextmanager
import asyncio
import os
import signal
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles

from server.audit import install_redacting_filter
from server.auth.middleware import IdentityMiddleware, current_identity
from server.bridge.ws import build_ws_router
from server.db import init_db
from server.reauth import ReauthManager
from server.sessions.orchestrator import SessionOrchestrator

# API routers (T036–T038)
from server.api.sessions import router as sessions_router
from server.api.accounts import router as accounts_router
from server.api.auth import router as auth_router
from server.api.config import router as config_router
from server.api.repos import router as github_router
from server.api.settings import router as settings_router


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
    await app.state.orchestrator.rehydrate()

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

    # Start the singleton orchestrator (usage poller + rotation monitor)
    await app.state.orchestrator.start()
    yield
    # Shutdown
    await app.state.reauth.shutdown()
    await app.state.orchestrator.stop()


def create_app(
    orchestrator: SessionOrchestrator | None = None,
    reauth_manager: ReauthManager | None = None,
) -> FastAPI:
    app = FastAPI(title="ccswitch", version="0.1.0", lifespan=_lifespan)

    # Attach the singleton orchestrator (allows tests to inject a custom one)
    if orchestrator is None:
        orchestrator = SessionOrchestrator()
    app.state.orchestrator = orchestrator

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

    # REST API routers (T036–T038)
    app.include_router(sessions_router)
    app.include_router(accounts_router)
    app.include_router(auth_router)
    app.include_router(config_router)
    app.include_router(github_router)
    app.include_router(settings_router)

    # WebSocket bridge
    ws_router = build_ws_router(orchestrator)
    app.include_router(ws_router)

    # SPA static files — optional; guard so it's missing in dev/test (T039)
    spa_dist = Path(__file__).parent.parent / "web" / "dist"
    if spa_dist.is_dir():
        app.mount("/", StaticFiles(directory=str(spa_dist), html=True), name="spa")

    return app


app = create_app()
