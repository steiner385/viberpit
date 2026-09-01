"""Host-aware orchestrator facade (spec: 2026-08-29-multi-host-runner-dispatch-design).

Becomes app.state.sessions in remote mode. Routes each edge->session call to the
right daemon; unrouted calls go to the primary host's REAL bound method, so sync/
async behaviour and the @rpc_deadline marker are preserved for free.
"""
import asyncio

from server.sessions.facade import _DEADLINE_ATTR
from server.sessions.fleet_router import FleetRouter, NoHostAvailable
from server.sessions.remote import RemoteOrchestrator

# Fan-out methods that take session_ids: list[str] and group calls by owning host,
# then merge the results. Each host receives only the IDs it owns.
_FANOUT_BY_IDS: frozenset[str] = frozenset({
    "latest_suggestions_many", "latest_summary_many", "prs_for_many",
})

# Method names whose FIRST positional arg is session_id and that touch live daemon
# state (must run on the owning host). Pinned by a completeness test (Task 4a).
_PER_SESSION: frozenset[str] = frozenset({
    "get_session", "send_prompt", "ensure_driver", "session_queue_move",
    "session_queue_cancel", "cancel_queued", "archive_session", "permanent_delete",
    "rename_session", "note_user_interaction", "set_autopilot_paused", "interrupt",
    "set_permission_mode", "resolve_permission", "answer_question", "pending_questions",
    "pending_permissions", "transcript_path", "transcript_events",
    "resolve_transcript_path", "transcript_window", "transcript_search_text",
    "command_catalog", "run_command", "latest_suggestions", "generate_suggestions_now",
    "latest_summary", "pop_pending_initial_prompt", "pop_pending_initial_attachment_ids",
    "request_pr_fix", "prs_for", "ring_covers", "ring_tail", "unarchive",
    "x11_open_browser", "resolve_model_approval",
})


def _orch_for_endpoint(endpoint: str):
    """Build a RemoteOrchestrator for a non-primary host endpoint.

    Called once per enabled non-primary Host row at startup. The edge's client TLS
    config (one CA signs every daemon cert) is picked up automatically by DaemonClient.
    """
    from server.ipc.client import DaemonClient
    from server.ipc.paths import parse_endpoint
    return RemoteOrchestrator(DaemonClient(parse_endpoint(endpoint)))


async def build_fleet_router(primary_name: str, primary_orch) -> FleetRouter:
    """Populate a FleetRouter with one orchestrator per enabled Host row.

    The primary reuses the already-built orchestrator (its client is already live).
    All other enabled hosts get a fresh RemoteOrchestrator via _orch_for_endpoint.
    """
    from sqlalchemy import select as _sel
    from server.db import db_session
    from server.db.models import Host

    router = FleetRouter()
    router.add(primary_name, primary_orch)
    async with db_session() as db:
        rows = (await db.execute(
            _sel(Host).where(Host.enabled.is_(True)))).scalars().all()
    for r in rows:
        if r.name == primary_name:
            continue
        router.add(r.name, _orch_for_endpoint(r.endpoint))
    return router


class FleetOrchestrator:
    def __init__(self, router: FleetRouter, primary: str) -> None:
        self._router = router
        self._primary = primary
        self._host_cache: dict[str, str] = {}  # session_id -> host

    def _primary_orch(self):
        return self._router.get(self._primary)

    async def _host_for(self, session_id: str) -> str | None:
        cached = self._host_cache.get(session_id)
        if cached is not None:
            return cached
        from sqlalchemy import select as _sel
        from server.db import db_session
        from server.db.models import Session as _S
        async with db_session() as db:
            host = (await db.execute(
                _sel(_S.host).where(_S.id == session_id))).scalar_one_or_none()
        if host:
            self._host_cache[session_id] = host
        return host

    def _orch_for_session_sync(self, session_id: str):
        host = self._host_cache.get(session_id)
        try:
            return self._router.get(host) if host else self._primary_orch()
        except Exception:
            return self._primary_orch()

    def _make_per_session(self, name: str):
        async def _call(session_id, *a, **k):
            host = await self._host_for(session_id)
            try:
                orch = self._router.get(host) if host else self._primary_orch()
            except Exception:
                orch = self._primary_orch()
            return await getattr(orch, name)(session_id, *a, **k)
        if getattr(getattr(RemoteOrchestrator, name, None), _DEADLINE_ATTR, False):
            setattr(_call, _DEADLINE_ATTR, True)
        return _call

    async def create_session(self, *, prefer: str | None = None, **kwargs):
        order = await self._router.pick(prefer=prefer)  # raises NoHostAvailable if empty
        last_exc: Exception | None = None
        for host in order:
            try:
                rec = await self._router.get(host).create_session(**kwargs)
            except Exception as e:  # spawn/clone/account errors → try next ranked host
                last_exc = e
                continue
            self._host_cache[rec.id] = host
            await self._stamp_host(rec.id, host)
            try:
                rec.host = host
            except Exception:  # pragma: no cover — frozen record types
                pass
            return rec
        raise last_exc if last_exc is not None else NoHostAvailable("no host accepted the session")

    async def _stamp_host(self, session_id: str, host: str) -> None:
        from sqlalchemy import update as _upd
        from server.db import db_session
        from server.db.models import Session as _S
        async with db_session() as db:
            await db.execute(_upd(_S).where(_S.id == session_id).values(host=host))
            await db.commit()

    def _make_fanout(self, name: str):
        async def _call(session_ids, *a, **k):
            groups: dict[str, list[str]] = {}
            for sid in session_ids:
                host = await self._host_for(sid) or self._primary
                groups.setdefault(host, []).append(sid)

            async def _one(host, subset):
                try:
                    orch = self._router.get(host)
                except Exception:
                    orch = self._primary_orch()
                return await getattr(orch, name)(subset, *a, **k)

            merged: dict = {}
            for res in await asyncio.gather(*(_one(h, s) for h, s in groups.items())):
                merged.update(res or {})
            return merged
        if getattr(getattr(RemoteOrchestrator, name, None), _DEADLINE_ATTR, False):
            setattr(_call, _DEADLINE_ATTR, True)
        return _call

    def subscribe(self, session_id: str):
        # Sync per-session iterator. The WS handler calls ensure_driver (async, warms
        # the cache) before subscribe, so the cache is populated by now; on a miss we
        # fall back to primary rather than block on a DB read from a sync method.
        return self._orch_for_session_sync(session_id).subscribe(session_id)

    def __getattr__(self, name: str):
        if name in _FANOUT_BY_IDS:
            return self._make_fanout(name)
        if name in _PER_SESSION:
            return self._make_per_session(name)
        return getattr(self._primary_orch(), name)
