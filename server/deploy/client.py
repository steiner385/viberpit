from __future__ import annotations
import httpx


class DeployControllerUnavailable(Exception):
    """The deploy controller socket could not be reached."""


class DeployControllerClient:
    def __init__(self, socket_path: str):
        self._sock = socket_path
        self._http = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=socket_path),
            base_url="http://deploy", timeout=10.0)

    async def get_status(self) -> dict:
        try:
            r = await self._http.get("/status")
        except httpx.ConnectError as exc:
            raise DeployControllerUnavailable(str(exc)) from exc
        return r.json()

    async def start(self) -> tuple[int, dict]:
        try:
            r = await self._http.post("/start")
        except httpx.ConnectError as exc:
            raise DeployControllerUnavailable(str(exc)) from exc
        return r.status_code, r.json()

    async def healthz(self) -> bool:
        try:
            r = await self._http.get("/healthz")
            return r.status_code == 200
        except httpx.ConnectError:
            return False
