from __future__ import annotations
import os

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from server.audit import record_audit
from server.auth.middleware import current_identity
from server.db import get_db
from server.deploy.client import DeployControllerClient, DeployControllerUnavailable
from server import hostpaths

router = APIRouter(prefix="/api/deploy", tags=["deploy"])


def get_deploy_client(request: Request) -> DeployControllerClient:
    client = getattr(request.app.state, "deploy_client", None)
    if client is None:
        sock = os.environ.get("CCSWITCH_DEPLOY_SOCKET", hostpaths.DEPLOY_SOCKET)
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
    identity: str = Depends(current_identity),
    client: DeployControllerClient = Depends(get_deploy_client),
    db=Depends(get_db),
) -> JSONResponse:
    # A deploy is privileged and outward-facing, so record WHO asked for it. The
    # identity is already resolved here for authorisation; auditing it is what makes
    # an unexpected deploy attributable after the fact. Refused and unavailable
    # attempts are recorded too - a preflight rejection is precisely the shape an
    # unexpected trigger takes, so dropping it would lose the interesting case.
    #
    # NOTE: this covers deploys that arrive through the edge. The controller's unix
    # socket accepts /start directly, and such a caller never reaches this route.
    target = body.targetSha if body is not None else None
    try:
        code, resp = await client.start(target)
    except DeployControllerUnavailable:
        await record_audit(db=db, type="deploy_start", identity=identity,
                           detail={"targetSha": target, "started": False,
                                   "reason": "controller unavailable"})
        return JSONResponse({"started": False, "reason": "controller unavailable"}, status_code=503)
    await record_audit(db=db, type="deploy_start", identity=identity,
                       detail={"targetSha": target, "status": code,
                               "started": bool(resp.get("started")),
                               "reason": resp.get("reason")})
    return JSONResponse(resp, status_code=code)
