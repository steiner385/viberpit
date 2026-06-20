from contextlib import asynccontextmanager
import os

from fastapi import Depends, FastAPI

from server.audit import install_redacting_filter
from server.auth.middleware import IdentityMiddleware, current_identity
from server.bridge.ws import build_ws_router
from server.db import init_db
from server.sessions.orchestrator import SessionOrchestrator


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

    # WebSocket bridge
    ws_router = build_ws_router(orchestrator)
    app.include_router(ws_router)

    return app


app = create_app()
