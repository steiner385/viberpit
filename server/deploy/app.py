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

from server.deploy import git as _git
from server.deploy.controller import DeployController
from server.deploy.detect import DetectLoop, compute_pending, edge_live_sha
from server.deploy.steps import err_reason
from server.deploy.types import DeployStatus

log = logging.getLogger("server.deploy.app")


class StartBody(BaseModel):
    """Optional /start body: an explicit deploy target (the green-frontier sha) —
    absent/None means "origin/master's tip", today's behaviour."""
    targetSha: str | None = None


def _poll_interval() -> float:
    """Detect-loop poll cadence in seconds — VIBERPIT_DEPLOY_POLL_INTERVAL or 30s."""
    try:
        return float(os.environ.get("VIBERPIT_DEPLOY_POLL_INTERVAL", "30"))
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
                         status_path: str | None = None,
                         init_db_url: str | None = None,
                         repo_slug: str | None = None):
    # The edge's Pipeline attributes this controller's tip verdict to a repository by
    # this slug (server/pipeline/lane_health.py). Derived once from the checkout's
    # origin remote; a checkout without one is not fatal — the main lane simply
    # stays blind for it, which is honest, so log and carry on rather than refuse
    # to serve deploys.
    if repo_slug is None:
        try:
            repo_slug = _git.origin_slug(repo)
        except Exception as e:
            log.warning("origin slug for %s unknown (%s) — the Pipeline main lane "
                        "cannot attribute this controller's verdict", repo, err_reason(e))
            repo_slug = ""
    # liveSha comes from the RUNNING edge (/healthz), not the checkout's HEAD — a
    # checkout fast-forwarded without a unit restart must show as behind, not
    # deployed (#859). edge_live_sha falls back to checkout HEAD when unreachable.
    loop = detect_loop or DetectLoop(
        repo, interval_s=_poll_interval(),
        compute=functools.partial(compute_pending,
                                  live_sha_fn=functools.partial(edge_live_sha, health_url)))
    ctrl = controller or DeployController(repo, health_url=health_url, venv_python=venv_python,
                                          daemon_enabled=daemon_enabled,
                                          status_path=status_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # The controller reads the DB (load_runner_targets, record_deploy), so the engine
        # must exist — and it must be created HERE, on the loop uvicorn runs, never in a
        # separate asyncio.run: an asyncpg pool is loop-bound and fails on first use if it
        # was built on another loop. Tests pass no init_db_url and touch no DB.
        # None → this caller wants no DB at all (the tests). An empty string → init, and
        # let init_db fall back to settings.db_url, matching `python -m server.deploy
        # --once`. Production passes os.environ.get("VIBERPIT_DB_URL", ""), so an unset
        # env var still initialises rather than silently leaving the runners phase and
        # record_deploy without an engine.
        if init_db_url is not None:
            from server.db import init_db
            await init_db(db_url=init_db_url or None)
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
        return {"repo": repo_slug, "pending": loop.state.to_dict(), "deploy": _deploy_dict()}

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
