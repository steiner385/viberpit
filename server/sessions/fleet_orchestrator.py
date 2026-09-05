"""Host-aware orchestrator facade (spec: 2026-08-29-multi-host-runner-dispatch-design).

Becomes app.state.sessions in remote mode. Routes each edge->session call to the
right daemon; unrouted calls go to the primary host's REAL bound method, so sync/
async behaviour and the @rpc_deadline marker are preserved for free.
"""
import asyncio
import logging

from ccswitch.quota import QuotaSnapshot
from server.codex.quota import persist_quota_snapshot
from server.sessions.facade import _DEADLINE_ATTR
from server.sessions.fleet_router import FleetRouter, NoHostAvailable
from server.sessions.engine import AgentEngine, AgentEngineUnavailable, parse_agent_engine
from server.sessions.remote import RemoteOrchestrator
from server.sessions.types import is_assistant, is_assistant_kind

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


def _merge_codex_status(parts: list[dict]) -> dict:
    """Reduce per-host Codex account status into one fleet answer.

    ``activeRunners`` genuinely spans the fleet — rocky may be running the
    account's app-server while dobby is not — so a primary-only read would report
    0 for an account that is very much in use. That is the #920 shape, which is
    why this is a merge-all rather than a primary-local read.

    ``quota`` is account-scoped, so every host that answers reports the same
    windows; the freshest canonical snapshot (preferring available) wins.
    ``degradedReason`` is per-host and the first one found is surfaced: one
    degraded runner is worth showing even when the others are healthy.
    """
    best: dict | None = None
    runners = 0
    degraded: str | None = None
    for part in parts:
        runners += int((part or {}).get("activeRunners") or 0)
        degraded = degraded or (part or {}).get("degradedReason")
        quota = (part or {}).get("quota")
        if not isinstance(quota, dict):
            continue
        if best is None:
            best = quota
            continue
        # ISO UTC timestamps sort chronologically; negate nothing by choosing the
        # lexically greater timestamp when availability is equal.
        rank = (quota.get("availability") == "available", str(quota.get("fetched_at") or ""))
        best_rank = (best.get("availability") == "available", str(best.get("fetched_at") or ""))
        if rank > best_rank:
            best = quota
    return {"quota": best, "activeRunners": runners, "degradedReason": degraded}


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
    "resolve_transcript_path", "transcript_window", "engine_replay_window",
    "transcript_search_text",
    "command_catalog", "run_command", "latest_suggestions", "generate_suggestions_now",
    "latest_summary", "pop_pending_initial_prompt", "pop_pending_initial_attachment_ids",
    "request_pr_fix", "prs_for", "ring_covers", "ring_tail", "unarchive",
    "x11_open_browser", "resolve_model_approval", "resume_interrupted",
    "reload_session", "rotate_to_best", "set_account_pin",
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
        if "engine" in kwargs:
            engine = parse_agent_engine(kwargs["engine"])
            if engine is AgentEngine.CLAUDE_CODE:
                # Preserve the exact legacy request for mixed-version runners.
                kwargs.pop("engine")
            else:
                # Protocol v1 has no host-engine capability advertisement. Never
                # gamble a Codex request on an arbitrary mixed-version runner.
                raise AgentEngineUnavailable(engine)
        # The assistant is pinned to the primary: its tools call the edge's own loopback
        # API and its in-process MCP server lives in the primary daemon, so a worker
        # placement would have neither. Overrides any caller preference.
        if is_assistant_kind(kwargs.get("kind")):
            prefer = self._primary
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

    async def list_sessions(self, *, include_assistant: bool = False):
        """Fleet-wide session list: ask EVERY host and merge.

        ``include_assistant`` is forwarded to every host (each applies the same
        ``is_assistant`` filter), and re-applied to the merged result so a mirror
        served by a host too old to know the flag is filtered here too.

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
        kw = {"include_assistant": True} if include_assistant else {}

        async def _one(name: str):
            try:
                return name, await self._router.get(name).list_sessions(**kw), None
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
                if not include_assistant and is_assistant(rec):
                    continue
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

    async def codex_invalidate_credentials(self, account: str) -> dict:
        """Revoke *account*'s Codex credentials on EVERY host, reporting per host.

        Deliberately not routed through ``_MERGE_ALL_HOSTS``. That helper skips an
        unreachable worker with a warning, which is right for an aggregate *read* —
        an undercounted session total is cosmetic — and wrong here: a host that did
        not answer may still be running an app-server with a live envelope, and
        reporting that as a successful revocation is the difference between
        "disabled" and "disabled everywhere". So every host's outcome is named, and
        a caller with a non-empty ``failed`` must surface the account as degraded
        and retry rather than call it disabled.
        """
        async def _one(name: str):
            try:
                return name, await self._router.get(name).codex_invalidate_credentials(
                    account
                ), None
            except Exception as e:  # noqa: BLE001 — one host must not block the rest
                return name, None, e

        invalidated: list[str] = []
        failed: dict[str, str] = {}
        for name, _res, exc in await asyncio.gather(
            *(_one(n) for n in self._router.names())
        ):
            if exc is None:
                invalidated.append(name)
            else:
                failed[name] = str(exc)
                log.error("fleet.codex_invalidate_credentials host=%s account=%s "
                          "FAILED — the account may still be usable there",
                          name, account, exc_info=exc)
        return {"invalidated": invalidated, "failed": failed}

    async def codex_account_status(self, account: str) -> dict:
        """Merge every runner's status, then persist once from the edge."""

        async def _one(host: str):
            try:
                result = await self._router.get(host).codex_account_status(
                    account, persist=False
                )
                return host, result, None
            except Exception as exc:  # noqa: BLE001 -- isolate worker read failure
                return host, None, exc

        parts: list[dict] = []
        for host, result, exc in await asyncio.gather(
            *(_one(name) for name in self._router.names())
        ):
            if exc is not None:
                if host == self._primary:
                    raise exc
                log.warning(
                    "fleet.codex_account_status host=%s unreachable; skipping",
                    host,
                    exc_info=exc,
                )
                continue
            parts.append(result or {})
        merged = _merge_codex_status(parts)
        raw_quota = merged.get("quota")
        if isinstance(raw_quota, dict):
            snapshot = QuotaSnapshot.from_dict(raw_quota)
            await asyncio.to_thread(persist_quota_snapshot, account, snapshot)
        return merged

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
