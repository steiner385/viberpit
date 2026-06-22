"""Edge-side proxy that drives a SessionOrchestrator living in the session daemon.

Methods are async (they await RPC) even where the in-process orchestrator method
is sync; reconciling that with the sync API callers is an A2.2 concern. A2.1 wires
the vertical-slice methods only.
"""
from server.sessions.types import SessionRecord


class RemoteDriver:
    """Driver-shaped shim so ws.py can consume a remote session like a local one.

    ws.py only needs ``.events()`` (an async generator of wire dicts) and a
    non-None object; this wraps the daemon's per-session event stream.
    """
    def __init__(self, client, session_id: str):
        self._client = client
        self._session_id = session_id

    async def events(self):
        async for wire in self._client.subscribe(self._session_id):
            yield wire


class RemoteOrchestrator:
    def __init__(self, client):
        self._client = client

    async def list_sessions(self) -> list[SessionRecord]:
        r = await self._client.call("list_sessions")
        return [SessionRecord.from_dict(d) for d in r["sessions"]]

    async def get_session(self, session_id: str) -> SessionRecord | None:
        r = await self._client.call("get_session", session_id=session_id)
        return SessionRecord.from_dict(r["session"]) if r["session"] is not None else None

    async def create_session(self, **kwargs) -> SessionRecord:
        r = await self._client.call("create_session", **kwargs)
        return SessionRecord.from_dict(r["session"])

    async def send_prompt(self, session_id: str, text: str, images=None) -> None:
        await self._client.call("send_prompt", session_id=session_id, text=text, images=images)

    async def ensure_driver(self, session_id: str, identity: str = "system") -> "RemoteDriver | None":
        r = await self._client.call("ensure_driver", session_id=session_id, identity=identity)
        return RemoteDriver(self._client, session_id) if r["found"] else None

    def subscribe(self, session_id: str):
        return self._client.subscribe(session_id)

    async def aclose(self) -> None:
        await self._client.aclose()
