from __future__ import annotations
import os

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from server.auth.middleware import current_identity
from server.deploy.client import DeployControllerClient, DeployControllerUnavailable

router = APIRouter(prefix="/api/deploy", tags=["deploy"])


def get_deploy_client(request: Request) -> DeployControllerClient:
    client = getattr(request.app.state, "deploy_client", None)
    if client is None:
        sock = os.environ.get("CCSWITCH_DEPLOY_SOCKET", "/run/ccswitch/deploy.sock")
        client = DeployControllerClient(sock)
        request.app.state.deploy_client = client
    return client


async def _reconcile_restore_failed(request: Request, status: dict) -> None:
    """Overwrite the controller's frozen ``restore.failed`` with the daemon's live
    view. The controller records which sessions failed at deploy time and never
    updates; the daemon is the single source of truth for which are *still* not
    live, so the banner clears as recovery (auto-retry or manual open) drains them.
    Best-effort: a daemon hiccup leaves the controller's list untouched."""
    orch = getattr(request.app.state, "sessions", None)
    if orch is None or not hasattr(orch, "resume_recovery_status"):
        return
    try:
        live = await orch.resume_recovery_status()
    except Exception:
        return
    restore = status.setdefault("deploy", {}).setdefault("restore", {})
    restore["failed"] = live.get("failed", [])
    restore["gaveUp"] = live.get("gaveUp", [])


@router.get("/status")
async def deploy_status(
    request: Request,
    _identity: str = Depends(current_identity),
    client: DeployControllerClient = Depends(get_deploy_client),
) -> JSONResponse:
    try:
        status = await client.get_status()
        await _reconcile_restore_failed(request, status)
        return JSONResponse(status)
    except DeployControllerUnavailable:
        # 503, not a 200 carrying deploy.error="controller unavailable". The frontend reads any
        # deploy.error as a terminal "Upgrade failed", so a transient blip (controller event loop
        # briefly busy, or its socket momentarily unreachable mid-deploy) would flash a spurious
        # failure. A 503 keeps react-query on its last-good data + sets isError, routing the blip
        # through the existing "reconnecting…" path instead of misclassifying the deploy.
        return JSONResponse({"reason": "controller unavailable"}, status_code=503)


class DeployStartBody(BaseModel):
    """Optional start body: deploy an explicit sha (the newest GREEN pending commit,
    when origin/master's tip is red). Absent means the tip — today's behaviour."""
    targetSha: str | None = None


@router.post("/start")
async def deploy_start(
    body: DeployStartBody | None = None,
    _identity: str = Depends(current_identity),
    client: DeployControllerClient = Depends(get_deploy_client),
) -> JSONResponse:
    try:
        code, resp = await client.start(body.targetSha if body is not None else None)
        return JSONResponse(resp, status_code=code)
    except DeployControllerUnavailable:
        return JSONResponse({"started": False, "reason": "controller unavailable"}, status_code=503)
