"""Edge-side client for the session daemon over a Unix socket."""
import httpx


class DaemonUnavailable(Exception):
    """The daemon socket could not be reached."""


class DaemonError(Exception):
    """The daemon returned an error envelope for an RPC call."""

    def __init__(self, err_type: str, message: str):
        super().__init__(f"{err_type}: {message}")
        self.err_type = err_type
        self.message = message


class DaemonClient:
    def __init__(self, socket_path: str):
        self._sock = socket_path
        self._http = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=socket_path),
            base_url="http://daemon",
            timeout=30.0,
        )

    async def call(self, method: str, **params) -> dict:
        try:
            resp = await self._http.post(f"/rpc/{method}", json=params)
        except httpx.ConnectError as exc:
            raise DaemonUnavailable(str(exc)) from exc
        body = resp.json()
        if "error" in body:
            err = body["error"]
            raise DaemonError(err.get("type", "Error"), err.get("message", ""))
        return body["result"]

    async def aclose(self) -> None:
        await self._http.aclose()
