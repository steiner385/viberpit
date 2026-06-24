from __future__ import annotations
import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Coroutine, Protocol, runtime_checkable

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from server.deploy.controller import DeployController
from server.deploy.detect import DetectLoop
from server.deploy.types import DeployStatus

log = logging.getLogger("server.deploy.app")


@runtime_checkable
class _ControllerLike(Protocol):
    status: DeployStatus

    def deploy_once(self) -> Coroutine[object, object, DeployStatus]: ...


def build_controller_app(*, repo: str, health_url: str, venv_pip: str, daemon_enabled: bool,
                         detect_loop: DetectLoop | None = None,
                         controller: _ControllerLike | None = None):
    loop = detect_loop or DetectLoop(repo)
    ctrl = controller or DeployController(repo, health_url=health_url, venv_pip=venv_pip,
                                          daemon_enabled=daemon_enabled)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(loop.run(), name="deploy-detect-loop")
        try:
            yield
        finally:
            task.cancel()

    app = FastAPI(lifespan=lifespan)

    def _deploy_dict() -> dict:
        s = ctrl.status
        return {"running": s.running, "phases": s.phases, "error": s.error,
                "warnings": s.warnings, "targetSha": s.target_sha}

    @app.get("/status")
    async def status() -> dict:
        return {"pending": loop.state.to_dict(), "deploy": _deploy_dict()}

    @app.post("/start")
    async def start() -> JSONResponse:
        if ctrl.status.running:
            return JSONResponse({"started": False, "reason": "in progress"}, status_code=409)
        task = asyncio.create_task(ctrl.deploy_once(), name="deploy-run")
        await asyncio.sleep(0)  # yield so the task can run its synchronous guard check
        if task.done() and task.exception() is not None:
            exc = task.exception()
            if isinstance(exc, RuntimeError):
                return JSONResponse({"started": False, "reason": "in progress"}, status_code=409)
        return JSONResponse({"started": True})

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    return app, loop, ctrl
