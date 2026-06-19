from contextlib import asynccontextmanager
import os

from fastapi import Depends, FastAPI

from server.audit import install_redacting_filter
from server.auth.middleware import IdentityMiddleware, current_identity
from server.db import init_db


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """App startup/shutdown: init DB, install log filter."""
    install_redacting_filter()
    db_url = os.environ.get("CCSWITCH_DB_URL", "sqlite+aiosqlite:///:memory:")
    await init_db(db_url=db_url)
    yield
    # Shutdown: connection pools clean up automatically


def create_app() -> FastAPI:
    app = FastAPI(title="ccswitch", version="0.1.0", lifespan=_lifespan)

    # Identity middleware — wraps all routes
    app.add_middleware(IdentityMiddleware)

    @app.get("/api/health")
    async def health(_identity: str = Depends(current_identity)) -> dict:
        return {"ok": True, "version": "0.1.0"}

    return app


app = create_app()
