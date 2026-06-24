from __future__ import annotations
import os

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

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


@router.get("/status")
async def deploy_status(
    _identity: str = Depends(current_identity),
    client: DeployControllerClient = Depends(get_deploy_client),
) -> dict:
    try:
        return await client.get_status()
    except DeployControllerUnavailable:
        return {
            "pending": {"behind": False, "commits": [], "ci": "unavailable"},
            "deploy": {
                "running": False,
                "phases": {},
                "error": "controller unavailable",
                "warnings": [],
                "targetSha": "",
            },
        }


@router.post("/start")
async def deploy_start(
    _identity: str = Depends(current_identity),
    client: DeployControllerClient = Depends(get_deploy_client),
) -> JSONResponse:
    try:
        code, body = await client.start()
        return JSONResponse(body, status_code=code)
    except DeployControllerUnavailable:
        return JSONResponse({"started": False, "reason": "controller unavailable"}, status_code=503)
