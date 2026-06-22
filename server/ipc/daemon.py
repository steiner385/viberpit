"""The session-daemon ASGI app: unary RPC registry + (Task 3) multiplexed event WS.

Served by uvicorn bound to a Unix socket. No TCP. No app-level auth — the socket's
filesystem permissions are the trust boundary.
"""
import json
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from server.ipc import protocol as proto


def create_daemon_app() -> FastAPI:
    app = FastAPI(title="ccswitch-sessiond")
    app.state.rpc = {}

    # stream_id -> set of connected WebSockets subscribed to it
    app.state.subscribers = {}

    async def publish(stream: str, event: dict) -> None:
        frame = proto.event_frame(stream, event)
        for ws in list(app.state.subscribers.get(stream, ())):
            try:
                await ws.send_text(_dumps(frame))
            except Exception:  # noqa: BLE001 — drop a dead/slow connection, keep publishing
                app.state.subscribers.get(stream, set()).discard(ws)

    app.state.publish = publish

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

    @app.websocket("/events")
    async def events(ws: WebSocket) -> None:
        await ws.accept()
        mine: set[str] = set()
        try:
            while True:
                frame = _loads(await ws.receive_text())
                stream = frame.get("stream")
                if frame.get("type") == proto.SUBSCRIBE and stream:
                    app.state.subscribers.setdefault(stream, set()).add(ws)
                    mine.add(stream)
                elif frame.get("type") == proto.UNSUBSCRIBE and stream:
                    app.state.subscribers.get(stream, set()).discard(ws)
                    mine.discard(stream)
        except WebSocketDisconnect:
            pass
        finally:
            for stream in mine:
                app.state.subscribers.get(stream, set()).discard(ws)

    register_rpc(app, "ping", _ping)
    register_rpc(app, "start_demo", _make_start_demo(app))
    register_rpc(app, "echo", _make_echo(app))
    return app


async def _ping(params: dict) -> dict:
    return {"pong": True}


def register_rpc(app: FastAPI, name: str, fn) -> None:
    """Register an `async def fn(params: dict) -> dict` handler under `name`."""
    app.state.rpc[name] = fn


def _dumps(obj: dict) -> str:
    return json.dumps(obj, separators=(",", ":"))


def _loads(raw: str) -> dict:
    return json.loads(raw)


def _make_start_demo(app: FastAPI):
    async def start_demo(params: dict) -> dict:
        stream, n = params["stream"], int(params["n"])
        for i in range(n):
            await app.state.publish(stream, {"type": "tick", "i": i})
        return {"emitted": n}
    return start_demo


def _make_echo(app: FastAPI):
    async def echo(params: dict) -> dict:
        await app.state.publish(params["stream"], {"type": "echo", "text": params["text"]})
        return {"ok": True}
    return echo
