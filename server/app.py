from contextlib import asynccontextmanager
import os
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles

from server.audit import install_redacting_filter
from server.auth.middleware import IdentityMiddleware, current_identity
from server.bridge.ws import build_ws_router
from server.db import init_db
from server.sessions.orchestrator import SessionOrchestrator

# API routers (T036–T038)
from server.api.sessions import router as sessions_router
from server.api.accounts import router as accounts_router
from server.api.auth import router as auth_router


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """App startup/shutdown: init DB, install log filter, start orchestrator."""
    install_redacting_filter()
    db_url = os.environ.get("CCSWITCH_DB_URL", "sqlite+aiosqlite:///:memory:")
    await init_db(db_url=db_url)

    # Start the singleton orchestrator (usage poller + rotation monitor)
    await app.state.orchestrator.start()
    yield
    # Shutdown
    await app.state.orchestrator.stop()


def create_app(orchestrator: SessionOrchestrator | None = None) -> FastAPI:
    app = FastAPI(title="ccswitch", version="0.1.0", lifespan=_lifespan)

    # Attach the singleton orchestrator (allows tests to inject a custom one)
    if orchestrator is None:
        orchestrator = SessionOrchestrator()
    app.state.orchestrator = orchestrator

    # Identity middleware — wraps all routes
    app.add_middleware(IdentityMiddleware)

    @app.get("/api/health")
    async def health(_identity: str = Depends(current_identity)) -> dict:
        return {"ok": True, "version": "0.1.0"}

    # REST API routers (T036–T038)
    app.include_router(sessions_router)
    app.include_router(accounts_router)
    app.include_router(auth_router)

    # WebSocket bridge
    ws_router = build_ws_router(orchestrator)
    app.include_router(ws_router)

    # SPA static files — optional; guard so it's missing in dev/test (T039)
    spa_dist = Path(__file__).parent.parent / "web" / "dist"
    if spa_dist.is_dir():
        app.mount("/", StaticFiles(directory=str(spa_dist), html=True), name="spa")

    return app


app = create_app()
