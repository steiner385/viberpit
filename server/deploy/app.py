from __future__ import annotations
import asyncio
import functools
import logging
import os
from contextlib import asynccontextmanager
from typing import Coroutine, Protocol, runtime_checkable

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from server.deploy.controller import DeployController, default_daemon_factory
from server.deploy.detect import DetectLoop, compute_pending, edge_live_sha
from server.deploy.types import DeployStatus

log = logging.getLogger("server.deploy.app")


class StartBody(BaseModel):
    """Optional /start body: an explicit deploy target (the green-frontier sha) —
    absent/None means "origin/master's tip", today's behaviour."""
    targetSha: str | None = None


def _poll_interval() -> float:
    """Detect-loop poll cadence in seconds — CCSWITCH_DEPLOY_POLL_INTERVAL or 30s."""
    try:
        return float(os.environ.get("CCSWITCH_DEPLOY_POLL_INTERVAL", "30"))
    except ValueError:
        return 30.0


@runtime_checkable
class _ControllerLike(Protocol):
    status: DeployStatus

    def deploy_once(self, target_sha: str | None = None
                    ) -> Coroutine[object, object, DeployStatus]: ...


def build_controller_app(*, repo: str, health_url: str, venv_python: str, daemon_enabled: bool,
                         detect_loop: DetectLoop | None = None,
                         controller: _ControllerLike | None = None,
                         status_path: str | None = None):
    # liveSha comes from the RUNNING edge (/healthz), not the checkout's HEAD — a
    # checkout fast-forwarded without a unit restart must show as behind, not
    # deployed (#859). edge_live_sha falls back to checkout HEAD when unreachable.
    loop = detect_loop or DetectLoop(
        repo, interval_s=_poll_interval(),
        compute=functools.partial(compute_pending,
                                  live_sha_fn=functools.partial(edge_live_sha, health_url)))
    ctrl = controller or DeployController(repo, health_url=health_url, venv_python=venv_python,
                                          daemon_enabled=daemon_enabled,
                                          daemon_client_factory=default_daemon_factory(daemon_enabled),
                                          status_path=status_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(loop.run(), name="deploy-detect-loop")
        try:
            yield
        finally:
            task.cancel()

    app = FastAPI(lifespan=lifespan)

    def _deploy_dict() -> dict:
        return ctrl.status.to_dict()

    @app.get("/status")
    async def status() -> dict:
        return {"pending": loop.state.to_dict(), "deploy": _deploy_dict()}

    @app.post("/start")
    async def start(body: StartBody | None = None) -> JSONResponse:
        if ctrl.status.running:
            return JSONResponse({"started": False, "reason": "in progress"}, status_code=409)
        target_sha = body.targetSha if body is not None else None
        task = asyncio.create_task(ctrl.deploy_once(target_sha=target_sha), name="deploy-run")
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
