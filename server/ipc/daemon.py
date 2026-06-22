"""The session-daemon ASGI app: unary RPC registry + (Task 3) multiplexed event WS.

Served by uvicorn bound to a Unix socket. No TCP. No app-level auth — the socket's
filesystem permissions are the trust boundary.
"""
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


def create_daemon_app() -> FastAPI:
    app = FastAPI(title="ccswitch-sessiond")
    app.state.rpc = {}

    @app.post("/rpc/{method}")
    async def rpc(method: str, request: Request) -> JSONResponse:
        fn = app.state.rpc.get(method)
        if fn is None:
            return JSONResponse(
                {"error": {"type": "UnknownMethod", "message": method}}, status_code=404)
        params = await request.json() if await request.body() else {}
        try:
            result = await fn(params)
            return JSONResponse({"result": result})
        except Exception as exc:  # noqa: BLE001 — surface handler errors as an envelope
            return JSONResponse(
                {"error": {"type": type(exc).__name__, "message": str(exc)}}, status_code=500)

    register_rpc(app, "ping", _ping)
    return app


async def _ping(params: dict) -> dict:
    return {"pong": True}


def register_rpc(app: FastAPI, name: str, fn) -> None:
    """Register an `async def fn(params: dict) -> dict` handler under `name`."""
    app.state.rpc[name] = fn
