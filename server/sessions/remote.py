"""Edge-side proxy that drives a SessionOrchestrator living in the session daemon.

Methods are async (they await RPC) even where the in-process orchestrator method
is sync; reconciling that with the sync API callers is an A2.2 concern. A2.1 wires
the vertical-slice methods only.
"""
import asyncio
from ccswitch.oauth import Usage
from server.sessions.types import SessionRecord


class _WireEvent:
    """Wraps an already-serialized event dict so ws.py's e.to_wire() works."""
    def __init__(self, wire: dict):
        self._wire = wire

    def to_wire(self) -> dict:
        return self._wire


class RemoteDriver:
    """Driver-shaped shim so ws.py can consume a remote session like a local one.

    ws.py needs ``.events()`` (an async generator of objects with ``.to_wire()``),
    ``.current_status()`` (sync, returns str), and ``.queued()`` (sync, returns list).
    """
    def __init__(self, client, session_id: str, status="idle", queued=None, head_seq=None):
        self._client = client
        self._session_id = session_id
        self._status = status
        self._queued = queued or []
        self._head_seq = head_seq

    def current_status(self) -> str:
        return self._status

    def queued(self) -> list:
        return self._queued

    async def events(self):
        async for wire in self._client.subscribe(self._session_id, since=self._head_seq):
            yield _WireEvent(wire)


class RemoteOrchestrator:
    def __init__(self, client):
        self._client = client
        self._global_subs: set[asyncio.Queue] = set()
        self._global_pump: asyncio.Task | None = None

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
        if not r["found"]:
            return None
        return RemoteDriver(self._client, session_id, status=r["status"], queued=r["queued"], head_seq=r.get("head_seq"))

    async def archive_session(self, session_id):
        return await self._client.call("archive_session", session_id=session_id)

    async def permanent_delete(self, session_id):
        return await self._client.call("permanent_delete", session_id=session_id)

    async def rename_session(self, session_id, name):
        return await self._client.call("rename_session", session_id=session_id, name=name)

    async def interrupt(self, session_id):
        return await self._client.call("interrupt", session_id=session_id)

    async def set_permission_mode(self, session_id, mode):
        return await self._client.call("set_permission_mode", session_id=session_id, mode=mode)

    async def cancel_queued(self, session_id, index):
        return await self._client.call("cancel_queued", session_id=session_id, index=index)

    async def resolve_permission(self, session_id, perm_id, decision, identity):
        return await self._client.call("resolve_permission", session_id=session_id, perm_id=perm_id,
                                        decision=decision, identity=identity)

    async def answer_question(self, session_id, question_id, selected, identity, mode="option"):
        return await self._client.call("answer_question", session_id=session_id, question_id=question_id,
                                        selected=selected, identity=identity, mode=mode)

    async def transcript_path(self, session_id):
        return await self._client.call("transcript_path", session_id=session_id)

    async def pending_questions(self, session_id):
        return await self._client.call("pending_questions", session_id=session_id)

    async def pop_pending_initial_prompt(self, session_id):
        return await self._client.call("pop_pending_initial_prompt", session_id=session_id)

    async def pop_pending_initial_attachment_ids(self, session_id):
        return await self._client.call("pop_pending_initial_attachment_ids", session_id=session_id)

    async def command_catalog(self, session_id):
        return await self._client.call("command_catalog", session_id=session_id)

    async def run_command(self, session_id, name, args):
        return await self._client.call("run_command", session_id=session_id, name=name, args=args)

    async def list_archived(self):
        return await self._client.call("list_archived")

    async def unarchive(self, session_id):
        r = await self._client.call("unarchive", session_id=session_id)
        return SessionRecord.from_dict(r["session"]) if r["session"] is not None else None

    async def adopt_session(self, claude_session_id, identity):
        r = await self._client.call("adopt_session", claude_session_id=claude_session_id, identity=identity)
        return SessionRecord.from_dict(r["record"]), r["metadata"]

    async def transcript_events(self, session_id):
        r = await self._client.call("transcript_events", session_id=session_id)
        return r["events"]

    async def transcript_window(self, session_id, *, before=None, limit=20):
        """Resolve the transcript path via daemon, then replay locally."""
        from server.sessions.replay import replay_window
        path = await self.transcript_path(session_id)
        if not path:
            return None
        return await replay_window(path, before=before, limit=limit)

    async def usage(self, name):
        r = await self._client.call("usage", name=name)
        return Usage.from_dict(r["usage"]) if r["usage"] is not None else None

    async def pending_permissions(self, session_id):
        return await self._client.call("pending_permissions", session_id=session_id)

    async def active_session_count(self):
        return await self._client.call("active_session_count")

    async def subscribe_events(self) -> asyncio.Queue:
        """Return a queue that receives global lifecycle events.

        A single daemon subscription is shared; each caller gets their own
        unbounded queue and events are fanned out to all active subscribers.
        Call unsubscribe_events(q) to stop receiving events on a queue.
        """
        q: asyncio.Queue = asyncio.Queue()
        self._global_subs.add(q)
        if self._global_pump is None or self._global_pump.done():
            async def _pump():
                async for ev in self._client.subscribe("_global"):
                    for sq in list(self._global_subs):
                        sq.put_nowait(ev)
            self._global_pump = asyncio.create_task(_pump(), name="global-pump")
        return q

    async def unsubscribe_events(self, q: asyncio.Queue) -> None:
        """Remove q from the fan-out set; the shared pump continues for remaining subscribers."""
        self._global_subs.discard(q)

    def subscribe(self, session_id: str):
        return self._client.subscribe(session_id)

    async def aclose(self) -> None:
        if self._global_pump is not None and not self._global_pump.done():
            self._global_pump.cancel()
        self._global_subs.clear()
        await self._client.aclose()
