from __future__ import annotations

import os

import httpx

from server import hostpaths
from server.http_keepalive import CLIENT_KEEPALIVE_EXPIRY_S

# httpx's defaults with the keep-alive expiry pinned to the shared constant: the
# controller's uvicorn (server/deploy/serve.py) must outlive it, or the UI's 5 s
# deploy-status poll rides the same race the daemon hop did (#1614).
_LIMITS = httpx.Limits(max_connections=100, max_keepalive_connections=20,
                       keepalive_expiry=CLIENT_KEEPALIVE_EXPIRY_S)


class DeployControllerUnavailable(Exception):
    """The deploy controller socket could not be reached."""


def default_socket_path() -> str:
    """The controller's unix socket: the env override, else the host default."""
    return os.environ.get("CCSWITCH_DEPLOY_SOCKET", hostpaths.DEPLOY_SOCKET)


class DeployControllerClient:
    def __init__(self, socket_path: str):
        self._sock = socket_path
        self._http = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=socket_path),
            base_url="http://deploy", timeout=10.0, limits=_LIMITS)

    async def get_status(self) -> dict:
        try:
            r = await self._http.get("/status")
        except httpx.HTTPError as exc:
            raise DeployControllerUnavailable(str(exc)) from exc
        return r.json()

    async def start(self, target_sha: str | None = None) -> tuple[int, dict]:
        try:
            if target_sha is None:
                # No target keeps today's empty-body POST byte for byte (older
                # controllers reject an unexpected JSON body with a 422).
                r = await self._http.post("/start")
            else:
                r = await self._http.post("/start", json={"targetSha": target_sha})
        except httpx.HTTPError as exc:
            raise DeployControllerUnavailable(str(exc)) from exc
        return r.status_code, r.json()

    async def healthz(self) -> bool:
        try:
            r = await self._http.get("/healthz")
            return r.status_code == 200
        except httpx.ConnectError:
            return False
