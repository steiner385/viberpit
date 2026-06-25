"""Edge-side proxy that drives a SessionOrchestrator living in the session daemon.

Methods are async (they await RPC) even where the in-process orchestrator method
is sync; reconciling that with the sync API callers is an A2.2 concern. A2.1 wires
the vertical-slice methods only.
"""
import asyncio
import logging
from ccswitch.oauth import Usage
from server.github.pr_status import PullRequestInfo
from server.sessions.types import SessionRecord, SYSTEM_IDENTITY

log = logging.getLogger(__name__)


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
        self._bg_tasks: set[asyncio.Task] = set()

    def current_status(self) -> str:
        return self._status

    def queued(self) -> list:
        return self._queued

    def queue_wire(self) -> list[dict]:
        """Structured queue snapshot, mirroring local SessionDriver.queue_wire so
        ws.py can re-emit queued-prompt chips on reattach in remote mode too.

        Without this, ws.py's `driver.queue_wire()` raised AttributeError on every
        remote attach (caught, but it meant queued prompts were invisible — a prompt
        typed while a turn was busy vanished with no feedback). The remote
        ensure_driver snapshot carries queued prompt *text* only (driver.queued()),
        not image refs, so images are omitted here; the live QueueUpdate events that
        stream over .events() carry the full wire shape with images."""
        return [{"text": t, "images": []} for t in self._queued]

    def seed_idle_suggestions(self, user_text: str, assistant_text: str) -> None:
        """Seed idle-session suggestions on the daemon-side driver (best-effort).

        Mirrors the local Driver.seed_idle_suggestions sync, fire-and-forget shape
        so the WS attach path (suggestions.kick_idle_suggestions) drives a remote
        session identically to a local one — without this, daemon mode raises
        AttributeError and idle sessions never get seeded suggestions. Schedules
        the RPC and returns immediately; any failure is logged, never raised.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no event loop to schedule on — skip (best-effort)
        task = loop.create_task(self._client.call(
            "seed_idle_suggestions",
            session_id=self._session_id,
            user_text=user_text,
            assistant_text=assistant_text,
        ))
        self._bg_tasks.add(task)

        def _done(t: asyncio.Task) -> None:
            self._bg_tasks.discard(t)
            if not t.cancelled() and t.exception() is not None:
                log.warning("RemoteDriver.seed_idle_suggestions RPC failed for %s: %s",
                            self._session_id, t.exception())

        task.add_done_callback(_done)

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

    async def pr_for(self, repo, branch) -> "PullRequestInfo | None":
        """Resolve a session's PR via the daemon's poller cache (None if absent)."""
        if not repo or not branch:
            return None
        r = await self._client.call("pr_for", repo=repo, branch=branch)
        info = r.get("pr")
        return PullRequestInfo.from_dict(info) if info else None

    async def prs_for(self, session_id: str) -> "list[PullRequestInfo]":
        """Resolve a session's full PR list via the daemon (empty if none)."""
        r = await self._client.call("prs_for", session_id=session_id)
        return [PullRequestInfo.from_dict(d) for d in (r.get("prs") or [])]

    async def get_session(self, session_id: str) -> SessionRecord | None:
        r = await self._client.call("get_session", session_id=session_id)
        return SessionRecord.from_dict(r["session"]) if r["session"] is not None else None

    async def create_session(self, **kwargs) -> SessionRecord:
        r = await self._client.call("create_session", **kwargs)
        return SessionRecord.from_dict(r["session"])

    async def send_prompt(self, session_id: str, text: str, images=None) -> str | None:
        r = await self._client.call("send_prompt", session_id=session_id, text=text, images=images)
        return (r or {}).get("status")

    async def ensure_driver(self, session_id: str, identity: str = SYSTEM_IDENTITY) -> "RemoteDriver | None":
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

    async def latest_suggestions(self, session_id: str) -> list[str]:
        r = await self._client.call("latest_suggestions", session_id=session_id)
        return r if isinstance(r, list) else []

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
        try:
            return await replay_window(path, before=before, limit=limit)
        except Exception:
            return None

    async def usage(self, name):
        r = await self._client.call("usage", name=name)
        return Usage.from_dict(r["usage"]) if r["usage"] is not None else None

    async def pending_permissions(self, session_id):
        return await self._client.call("pending_permissions", session_id=session_id)

    async def active_session_count(self):
        return await self._client.call("active_session_count")

    async def live_session_load(self):
        return await self._client.call("live_session_load")

    async def live_session_counts(self):
        return await self._client.call("live_session_counts")

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

    async def x11_metrics(self) -> dict:
        return await self._client.call("x11_metrics")

    async def x11_open_browser(self, session_id: str, url: str | None = None) -> dict:
        return await self._client.call("x11_open_browser", session_id=session_id, url=url)

    async def aclose(self) -> None:
        if self._global_pump is not None and not self._global_pump.done():
            self._global_pump.cancel()
        self._global_subs.clear()
        await self._client.aclose()
