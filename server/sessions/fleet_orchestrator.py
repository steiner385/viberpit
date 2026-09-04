"""Host-aware orchestrator facade (spec: 2026-08-29-multi-host-runner-dispatch-design).

Becomes app.state.sessions in remote mode. Routes each edge->session call to the
right daemon; unrouted calls go to the primary host's REAL bound method, so sync/
async behaviour and the @rpc_deadline marker are preserved for free.
"""
import asyncio
import logging

from server.sessions.facade import _DEADLINE_ATTR
from server.sessions.fleet_router import FleetRouter, NoHostAvailable
from server.sessions.remote import RemoteOrchestrator

log = logging.getLogger(__name__)

# Fan-out methods that take session_ids: list[str] and group calls by owning host,
# then merge the results. Each host receives only the IDs it owns.
_FANOUT_BY_IDS: frozenset[str] = frozenset({
    "latest_suggestions_many", "latest_summary_many", "prs_for_many",
})


def _merge_sum_floats(parts: list[dict]) -> dict[str, float]:
    """Sum weighted per-account loads across hosts (live_session_load)."""
    out: dict[str, float] = {}
    for d in parts:
        for k, v in (d or {}).items():
            out[k] = out.get(k, 0.0) + v
    return out


def _merge_sum_counts(parts: list[dict]) -> dict[str, dict[str, int]]:
    """Sum per-account {active, idle} count buckets across hosts (live_session_counts)."""
    out: dict[str, dict[str, int]] = {}
    for d in parts:
        for acct, buckets in (d or {}).items():
            cur = out.setdefault(acct, {"active": 0, "idle": 0})
            for key, n in (buckets or {}).items():
                cur[key] = cur.get(key, 0) + n
    return out


def _merge_union(parts: list[dict]) -> dict:
    """Union per-host maps keyed by session_id (session_queue_positions).

    Session ids are unique per host, so a plain overlay never clobbers a real
    entry; if the queue is one shared DB, every host returns the same map and the
    union is idempotent.
    """
    out: dict = {}
    for d in parts:
        out.update(d or {})
    return out


# Aggregate reads whose truthful value spans the WHOLE fleet: the primary's answer
# only covers sessions IT holds a driver for (live_session_load/counts) or its own
# queue (session_queue_positions), so a primary-only read silently undercounts every
# worker session (#920). Ask EVERY host and reduce with the per-method merge below.
_MERGE_ALL_HOSTS: dict[str, object] = {
    "live_session_load": _merge_sum_floats,
    "live_session_counts": _merge_sum_counts,
    "session_queue_positions": _merge_union,
}

# Method names whose FIRST positional arg is session_id and that touch live daemon
# state (must run on the owning host). Pinned by a completeness test (Task 4a).
_PER_SESSION: frozenset[str] = frozenset({
    "get_session", "send_prompt", "ensure_driver", "session_queue_move",
    "session_queue_cancel", "cancel_queued", "archive_session", "permanent_delete",
    "rename_session", "note_user_interaction", "set_autopilot_paused", "interrupt",
    "set_permission_mode", "resolve_permission", "answer_question", "pending_questions",
    "orphaned_interaction",
    "pending_permissions", "transcript_path", "transcript_events",
    "resolve_transcript_path", "transcript_window", "transcript_search_text",
    "command_catalog", "run_command", "latest_suggestions", "generate_suggestions_now",
    "latest_summary", "pop_pending_initial_prompt", "pop_pending_initial_attachment_ids",
    "request_pr_fix", "prs_for", "ring_covers", "ring_tail", "unarchive",
    "x11_open_browser", "resolve_model_approval", "resume_interrupted",
    "reload_session",
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

    def _make_merge_all(self, name: str, merge):
        """Fan a no-id aggregate read to EVERY host and reduce (#920).

        Host isolation mirrors ``list_sessions``: an unreachable non-primary host
        is skipped with a warning (a dead worker must not blank an aggregate), and
        the primary's own failure still raises so the API surfaces "daemon
        unavailable" rather than a silently partial total.
        """
        async def _call(*a, **k):
            async def _one(host: str):
                try:
                    return host, await getattr(self._router.get(host), name)(*a, **k), None
                except Exception as e:  # noqa: BLE001 — per-host isolation is the point
                    return host, None, e

            parts: list[dict] = []
            for host, res, exc in await asyncio.gather(
                    *(_one(n) for n in self._router.names())):
                if exc is not None:
                    if host == self._primary:
                        raise exc
                    log.warning("fleet.%s host=%s unreachable; skipping its share "
                                "this pass", name, host, exc_info=exc)
                    continue
                parts.append(res or {})
            return merge(parts)
        if getattr(getattr(RemoteOrchestrator, name, None), _DEADLINE_ATTR, False):
            setattr(_call, _DEADLINE_ATTR, True)
        return _call

    async def list_sessions(self):
        """Fleet-wide session list: ask EVERY host and merge.

        The primary's ``_records`` mirrors foreign rows only at rehydrate (process
        start), so a session a worker created after the primary's last restart was
        invisible to a primary-only list until the next restart — the "launched a
        session and it vanished" report. Merge rules:

        - a record served by the host that RUNS it (any non-primary host) beats the
          primary's mirror copy of the same id, so status is live, not stale;
        - a record with no ``host`` stamp is labelled with the host that served it,
          and that host is cached for per-session routing — pre-stamp rows carry
          host=NULL in the DB, which ``_host_for`` would otherwise route to the
          primary (→ 404 by id for a session that is alive on a worker);
        - an unreachable worker is skipped with a warning: a dead worker must not
          blank the whole dashboard;
        - the primary's failure still raises, so the API keeps surfacing "daemon
          unavailable" rather than a silently partial list;
        - a DRIVERLESS primary record stamped for another host that answered and
          did NOT list it is a stale mirror, not a session: the worker archived /
          deleted / closed it and popped its own ``_records``, but the primary's
          rehydrate-time copy lingers until the two-strike mirror reconcile
          (20–40 s; unbounded while the DB is unreachable). Listing it re-surfaced
          an archived card whose every action routed to the worker and 404'd
          "Session not found" — the zombie-session report. A record the primary
          actually drives (``detached`` False) is kept whatever its stamp.
        """
        names = self._router.names()

        async def _one(name: str):
            try:
                return name, await self._router.get(name).list_sessions(), None
            except Exception as e:  # noqa: BLE001 — per-host isolation is the point
                return name, None, e

        results = await asyncio.gather(*(_one(n) for n in names))
        merged: dict[str, object] = {}
        served_by: dict[str, str] = {}
        for name, recs, exc in results:
            if exc is not None:
                if name == self._primary:
                    raise exc
                log.warning("fleet.list_sessions host=%s unreachable; skipping its "
                            "sessions this pass", name, exc_info=exc)
                continue
            for rec in recs or []:
                prev = served_by.get(rec.id)
                # Keep an existing entry unless it is the primary's mirror and this
                # host is the one actually running the session.
                if prev is not None and not (prev == self._primary and name != self._primary):
                    continue
                if getattr(rec, "host", None) is None:
                    try:
                        rec.host = name
                    except Exception:  # pragma: no cover — frozen record types
                        pass
                if name != self._primary:
                    self._host_cache[rec.id] = name
                merged[rec.id] = rec
                served_by[rec.id] = name
        answered = {name for name, _recs, exc in results if exc is None}
        for sid, name in list(served_by.items()):
            if name != self._primary:
                continue  # served by the host that runs it — authoritative
            rec = merged[sid]
            owner = getattr(rec, "host", None)
            if (owner is not None and owner != self._primary and owner in answered
                    and getattr(rec, "detached", False)):
                log.info("fleet.list_sessions dropped stale primary mirror "
                         "session_id=%s owner=%s (owner answered without it)", sid, owner)
                del merged[sid]
        return list(merged.values())

    def subscribe(self, session_id: str):
        # Sync per-session iterator. The WS handler calls ensure_driver (async, warms
        # the cache) before subscribe, so the cache is populated by now; on a miss we
        # fall back to primary rather than block on a DB read from a sync method.
        return self._orch_for_session_sync(session_id).subscribe(session_id)

    def __getattr__(self, name: str):
        if name in _FANOUT_BY_IDS:
            return self._make_fanout(name)
        if name in _MERGE_ALL_HOSTS:
            return self._make_merge_all(name, _MERGE_ALL_HOSTS[name])
        if name in _PER_SESSION:
            return self._make_per_session(name)
        return getattr(self._primary_orch(), name)
