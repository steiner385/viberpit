"""Host-aware orchestrator facade (spec: 2026-08-29-multi-host-runner-dispatch-design).

Becomes app.state.sessions in remote mode. Routes each edge->session call to the
right daemon; unrouted calls go to the primary host's REAL bound method, so sync/
async behaviour and the @rpc_deadline marker are preserved for free.
"""
import asyncio
import logging
import math
import time

from viberpit import credential_envelope
from viberpit.quota import QuotaSnapshot
from server.ipc.client import DaemonUnavailable
from server.obs.logdedupe import LogThrottle, log_event_throttled
from server.usage.process_quota import persist_quota_snapshot
from server.sessions.display_policy import DISPLAY_VIRTUAL
from server.sessions.facade import _DEADLINE_ATTR
from server.sessions import host_probe, host_upgrade
from server.sessions.fleet_router import FleetRouter, NoEngineHostAvailable, NoHostAvailable
from server.sessions.host_engines import expected_contract_sha256
from server.sessions.engine import AgentEngine, parse_agent_engine
from server.sessions.remote import RemoteOrchestrator
from server.sessions.types import is_assistant, is_assistant_kind

log = logging.getLogger(__name__)

# Credential broker cadence (#2629) lives beside the runner store that honours it.
CREDS_PUSH_INTERVAL_S = credential_envelope.CREDS_PUSH_INTERVAL_S
CREDS_LEASE_S = credential_envelope.CREDS_LEASE_S
# One missed push costs nothing: the runner still holds a lease and the next tick refreshes it.
# A daemon restart window (~10-20 s, nothing listening) is one such miss (#2757). The lease is
# only at risk one tick before it lapses, so a run of refused/timed-out pushes is INFO until
# then and WARNING from then on. Derived, so the threshold and the cadence cannot drift apart.
CREDS_UNREACHABLE_ESCALATE_AFTER = math.ceil(CREDS_LEASE_S / CREDS_PUSH_INTERVAL_S) - 1

# Queue-drain nudge cadence: the fleet probe refreshes every host's
# ``active_sessions`` on this interval, so this is how often "a host freed a slot"
# can become visible to the edge at all.
QUEUE_DRAIN_INTERVAL_S = 30.0
# How often the edge finishes Archive Audit reopens the primary daemon handed it (#3142).
# The audit itself runs about daily, so a few minutes of hand-off latency is invisible.
ARCHIVE_REOPEN_INTERVAL_S = 300.0

# Worker status is an in-memory read, not an actionable-provider refresh. Bound
# its fan-out independently so an unavailable daemon cannot stall the overview.
AUTOPILOT_STATUS_TIMEOUT_S = 2.0

# --------------------------------------------------------------------------- #
# "A worker is unreachable, skip it" — ONE record family for every fan-out site.
#
# The site name used to be interpolated into the message text, and the error sink
# fingerprints a record on its NORMALIZED MESSAGE (server/obs/error_signature.py).
# So one condition — rocky powered off — was hashed five different ways and filed
# five auto-error issues: #1614 (codex_account_status), #1670 (session_queue_
# positions), #1671 (list_sessions), #1733 (live_session_load), #1734
# (live_session_counts). Same host, same outage, same remedy, five tickets.
#
# The site and the cause are still worth reading, so they are kept in the message
# and SINGLE-QUOTED: `normalize()` rewrites any quoted run to `<str>`, so they are
# carried to the journal and to the issue's sample verbatim while collapsing to one
# fingerprint. Quotes in the values themselves would defeat that, hence _field().
_UNREACHABLE_MSG = "fleet host=%s unreachable; skipping it this pass (site='%s' cause='%s')"

# Transport failures whose traceback is pure httpx/httpcore boilerplate: the RPC
# client already reduced them to a typed exception whose message IS the diagnosis
# ("All connection attempts failed"), so attaching exc_info adds 60 lines to every
# issue body and nothing to the reader. It also perturbs the fingerprint — the sink
# keys on the exception class and innermost app frame, both of which vary with how
# httpx happened to fail. Any OTHER exception type here is a bug, not an outage, and
# keeps its stack.
#
# ``OSError`` covers the raw socket failures a path that bypasses DaemonClient can
# raise, and with them ``TimeoutError`` — which since 3.11 is both an OSError subclass
# and what ``asyncio.TimeoutError`` aliases, so naming either separately is redundant.
_EXPECTED_UNREACHABLE = (DaemonUnavailable, OSError)

# Failures that, alone, read as a starved or restarting daemon rather than an outage:
# INFO until host_probe's run of consecutive misses escalates (#2821). ``TimeoutError``
# is the local ``asyncio.timeout()`` deadline around a worker read (automation_statuses),
# the same condition as an RPC-layer ``DaemonTimeout`` under a different type (#2846).
_ISOLATED_UNREACHABLE = (DaemonUnavailable, TimeoutError)


def _field(value: object) -> str:
    """Render *value* for a single-quoted log field: no quotes, no newlines."""
    return " ".join(str(value).replace("'", "").replace('"', "").split())


def _cause_text(exc: BaseException) -> str:
    """``Type: message`` for *exc*, falling back to its direct cause when bare.

    ``DaemonUnavailable(str(httpx_exc))`` normally carries the reason already; a
    handful of raise sites pass no message, and there the chained transport error
    is the only thing that says what went wrong.
    """
    text = _field(exc)
    if not text and exc.__cause__ is not None:
        return f"{type(exc).__name__}: {type(exc.__cause__).__name__}: {_field(exc.__cause__)}"
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


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


def _merge_probe(parts: list[dict]) -> dict:
    """Concatenate per-host probe results (probe_mcp): every host probes its own cfg dirs."""
    return {"rows": [r for p in parts for r in (p or {}).get("rows", [])],
            "skipped": [s for p in parts for s in (p or {}).get("skipped", [])]}


def _merge_sum_int(parts: list[int]) -> int:
    """Sum per-host counts (reconnect_mcp_everywhere)."""
    return sum(p or 0 for p in parts)


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
    "probe_mcp": _merge_probe,
    "reconnect_mcp_everywhere": _merge_sum_int,
    "live_session_load": _merge_sum_floats,
    "live_session_counts": _merge_sum_counts,
    "session_queue_positions": _merge_union,
}

# Method names whose FIRST positional arg is session_id and that touch live daemon
# state (must run on the owning host). Pinned by a completeness test (Task 4a).
_PER_SESSION: frozenset[str] = frozenset({
    "get_session", "send_prompt", "ensure_driver", "session_queue_move",
    "session_queue_cancel", "start_queued_now", "cancel_queued", "archive_session", "permanent_delete",
    "rename_session", "note_user_interaction", "set_autopilot_paused",
    # D-14 PR auto-fix opt-out: the row is shared, but the live record mirror is the
    # owning host's.
    "set_pr_autofix_paused",
    "set_auto_archive_opt_out", "dismiss_suspected_complete", "interrupt",
    "stop_subagent", "mcp_status_for", "mcp_reconnect", "mcp_toggle",
    # The diff reads the session's worktree, which lives on its owning host.
    "session_diff",
    # So do the D-7 file list and file read.
    "session_files", "session_file", "session_file_search",
    # D-21: a session's recovery refs live in its host's clone. An archived row routes
    # by its stamp too (`_host_for` reads row.host whatever the status).
    "session_recovery_refs",
    # The D-13 memory-file read: the loaded set, worktree and config dir are its host's.
    "memory_file",
    # D-18: a session's dev servers are its host's processes, and the preview opens on
    # its host's virtual display.
    "dev_servers", "open_dev_server",
    # D-19: a session's worktree is its host's — the Discard / Repair preflight and both
    # actions.
    "worktree_preflight", "discard_worktree", "repair_worktree",
    # A fork (D-8) is made from the source's transcript and worktree, on its host.
    "fork_session",
    # So is a rewind (D-9): the live client, transcript, worktree and undo backups.
    "rewind_session", "undo_rewind",
    # So is fast mode (D-14): the live client it is written to, and the record's model list.
    "set_fast_mode",
    # And so is an output style (D-16a): the live client, and the CLI's style list.
    "set_output_style",
    # And an MCP per-tool toggle (D-16b): the live client and the server's tool list.
    "set_mcp_tool",
    # Whether the owning daemon intercepts APP_ONLY commands (D-16a): a per-daemon
    # capability, so it must be asked of the host the prompt would go to.
    "has_app_command_gate",
    "set_permission_mode", "set_model_for_session",
    "resolve_permission", "answer_question", "pending_questions",
    "orphaned_interaction",
    "pending_permissions", "transcript_path", "transcript_source", "transcript_events",
    "resolve_transcript_path", "transcript_window", "engine_replay_window",
    "transcript_search_text", "transcript_search", "has_transcript",
    "command_catalog", "run_command", "latest_suggestions", "generate_suggestions_now",
    "latest_summary", "pop_pending_initial_prompt", "repark_pending_initial_prompt",
    # `post_notice` is NOT here — its first arg is an identity, not a session_id,
    # so it does not fit _make_per_session's calling convention; it falls through
    # __getattr__ to the primary instead (assistant sessions are pinned to primary
    # at create_session).
    "request_pr_fix", "prs_for", "ring_covers", "ring_tail", "unarchive",
    "x11_open_browser", "resolve_model_approval", "resume_interrupted",
    # The /ws/vnc stream must reach the daemon whose Xvfb draws the session, i.e. its
    # host's, exactly like x11_open_browser (a worker session's display is on the worker).
    "daemon_route_for_session",
    # The primary's chained ingest certifies a harvested one-shot session via its
    # edge; the status change must land where the record and driver are.
    "certify_harvested_complete",
    "reload_session", "rotate_to_best", "set_account_pin",
})


# Per-session calls that would build a driver or change live session state. While a
# move (D-17) is in flight the edge refuses them with SessionMoving (HTTP 409 /
# WS SESSION_MOVING) instead of letting one race the saga onto either host. Reads
# (get_session, transcripts, diffs) keep working and show ``movingTo``.
_MOVE_BLOCKED: frozenset[str] = frozenset({
    "send_prompt", "ensure_driver", "start_queued_now", "archive_session",
    "permanent_delete", "set_autopilot_paused", "mcp_reconnect", "mcp_toggle", "fork_session", "rewind_session", "undo_rewind",
    "set_fast_mode", "set_output_style", "set_mcp_tool", "set_permission_mode",
    "set_model_for_session", "resolve_permission", "answer_question", "run_command",
    "request_pr_fix", "unarchive", "x11_open_browser", "open_dev_server",
    "resolve_model_approval",
    "resume_interrupted", "certify_harvested_complete", "reload_session",
    "rotate_to_best", "set_account_pin", "generate_suggestions_now",
    # Live-record writes on the source host: landed mid-move, they would be lost with
    # the source's record (a worker forgets it) or race the target's import.
    "rename_session", "set_pr_autofix_paused", "set_auto_archive_opt_out",
    "dismiss_suspected_complete", "interrupt", "stop_subagent",
    # D-19: removing or recreating the worktree a move is carrying.
    "discard_worktree", "repair_worktree",
})
# Stopping a turn is refused only while a saga is LIVE (``_moving``): the session is
# idle then, so there is nothing to stop, and the stop would race the export. A HELD
# claim (a failed move, a dead edge's leftover, a host removed for good) can last
# indefinitely, and a daemon-local automation may start a turn on the lease holder
# meanwhile — refusing these then would leave that turn unstoppable (#3317).
_SAGA_ONLY_BLOCKED: frozenset[str] = frozenset({"interrupt", "stop_subagent"})
# A cached route whose host answers these with "no such session" is stale (the row
# was restamped behind this edge's back, #1432): drop it, re-read the stamp, retry once.
_NONE_MEANS_UNKNOWN: frozenset[str] = frozenset({"get_session", "ensure_driver"})


def _is_unknown_session_error(exc: BaseException) -> bool:
    """"No session with id …" from a host — in process (KeyError) or over the RPC
    envelope (DaemonError carrying the KeyError)."""
    from server.ipc.client import DaemonError
    if isinstance(exc, KeyError):
        return True
    return (isinstance(exc, DaemonError) and exc.err_type == "KeyError"
            and "No session with id" in exc.message)


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


def _describe_push_error(exc: BaseException) -> str:
    """``Type: message`` (plus the cause's type) -- ``httpx.ReadTimeout`` stringifies to ``""``."""
    text = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    cause = exc.__cause__ or exc.__context__
    return f"{text} (caused by {type(cause).__name__})" if cause is not None else text


class FleetOrchestrator:
    def __init__(self, router: FleetRouter, primary: str) -> None:
        self._router = router
        self._primary = primary
        self._host_cache: dict[str, str] = {}  # session_id -> host
        # Per-instance, not module-level: one fleet per process, and a fresh fleet
        # (a lifespan restart, a test) deserves a fresh window rather than the last
        # one's. Keyed by host name, so cardinality is the registry's size.
        self._unreachable_log = LogThrottle()
        # host -> consecutive creds_sync pushes it failed to answer (#2757).
        self._creds_unreachable: dict[str, int] = {}
        # session_id -> target host of a move saga running in THIS edge (D-17). The
        # edge guard reads it (SessionMoving) and every served record carries it as
        # ``moving_to``. The DB's ``sessions.moving_to`` is the durable twin.
        self._moving: dict[str, str] = {}
        # session_id -> target of a claim NO saga in this edge is running: a move that
        # ended ``failed``, or one a dead edge left behind. Held (driver builds answer
        # SessionMoving) until the reconcile settles it.
        self._held: dict[str, str] = {}
        # When this edge process started (naive UTC). One edge per deployment: a move
        # claim older than this can belong to no live saga, so the reconcile treats it
        # as stale at once instead of waiting out MOVE_STALE_S (D-17). Claims are
        # stamped with the DB's clock, so ``stamp_started_at`` replaces this local-clock
        # placeholder with the DB's once, at startup, before the reconcile's first pass.
        from server.db.clock import naive_utc_now
        self.started_at = naive_utc_now()
        # session_id -> monotonic time the reconcile first saw the target holding the
        # lease without the import marker (session_move.IMPORT_STALL_S).
        self._lease_wait: dict[str, float] = {}

    def _primary_orch(self):
        return self._router.get(self._primary)

    async def _log_unreachable(
        self, host: str, exc: BaseException, site: str
    ) -> None:
        """Skipping an unreachable worker is right either way; the LEVEL depends on why.

        Three policies, each answering a way this record used to become noise:

        * **A deliberate restart is not an error.** A worker mid-deploy is unreachable
          on purpose for 30–90 s, several times a day, and logging each failed poll in
          that window at WARNING fed Error Triage an issue about a host doing exactly
          what it was told (#1614). Those go out at INFO.
        * **Repeats are paced.** An unplanned outage is invisible to ``reachable_names``
          until the 30 s probe marks the host unhealthy, and every fan-out in that gap
          re-reports it. The shared windowed throttle reports the first occurrence
          promptly, demotes repeats inside the window to DEBUG (still in the journal,
          out of the error sink) and re-reports afterwards, so the record keeps saying
          "still down" without filing it again. The window is dropped on recovery
          (``_note_reachable``) so a host that flaps reports each outage promptly.
        * **One condition is one record.** The throttled WARNING below is a structured
          ``log_event_throttled`` record keyed on the dotted event name, not on
          ``_UNREACHABLE_MSG`` text: the sink fingerprints a plain record on its
          normalized message, and ``host`` — unlike the quoted ``site``/``cause`` —
          survives normalization, so this call site alone used to mint one auto-error
          issue per host (#2126). ``_UNREACHABLE_MSG`` remains only for the unthrottled
          INFO line below, which the sink never sees. A transport failure is summarized
          rather than carrying its httpx traceback (see ``_EXPECTED_UNREACHABLE``).

        What is deliberately NOT thinned here is ``host_probe``'s ``fleet.probe_failed``:
        the probe is the fleet's reachability DETECTOR and owns the canonical record,
        which is exactly why these call sites — consumers that merely notice what the
        probe is already reporting — can afford to be quiet. They stay at WARNING rather
        than INFO because they can fire in the gap before the next 30 s probe, or on a
        flap the probe slept through, and that is genuinely news the detector has not
        reported yet.
        """
        cause = _cause_text(exc)
        if await host_upgrade.restart_expected(host):
            # Unthrottled: a deploy's INFO lines are the record OF the deploy, and
            # sharing the warning's window would let a rollout's chatter demote the
            # genuine failure that follows it to DEBUG.
            log.info(_UNREACHABLE_MSG + " (restart in progress)",
                     host, _field(site), cause)
            return
        if (isinstance(exc, _ISOLATED_UNREACHABLE)
                and host_probe.unreachable_run(host) < host_probe.escalate_after(exc)):
            # A lone timeout/refusal is a starved or restarting daemon, not an outage:
            # the probe owns that policy (#2701/#2749), so stay INFO until its run of
            # consecutive misses escalates (#2821, #2846).
            log.info(_UNREACHABLE_MSG + " (isolated)", host, _field(site), cause)
            return
        expected = isinstance(exc, _EXPECTED_UNREACHABLE)
        log_event_throttled(
            log, self._unreachable_log, host, logging.WARNING,
            "fleet.host_unreachable", host=host, site=_field(site), cause=cause,
            exc_info=None if expected else exc,
        )

    def _note_reachable(self, host: str) -> None:
        """A host answered: drop its suppression window.

        Without this, a host that fails, recovers and fails again inside the window
        has its second outage demoted to DEBUG — the throttle would be pacing a
        *condition that ended*, which is the one thing a window must not do.
        """
        self._unreachable_log.forget(host)

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

    def _orch_for_host_or_primary(self, host: str | None):
        try:
            return self._router.get(host) if host else self._primary_orch()
        except Exception:
            return self._primary_orch()

    async def _reroute(self, session_id: str, host: str | None):
        """Self-heal a stale route (#1432, D-17): the cached host said it has no such
        session. Drop the entry and re-read the row's stamp; return the new host's
        orchestrator when the stamp moved, else None (nothing to retry)."""
        self._host_cache.pop(session_id, None)
        fresh = await self._host_for(session_id)
        if fresh is None or fresh == host:
            return None
        log.info("fleet.route_healed session=%s %s→%s", session_id, host, fresh)
        return self._orch_for_host_or_primary(fresh)

    def _make_per_session(self, name: str):
        async def _call(session_id, *a, **k):
            target = self._moving.get(session_id) or self._held.get(session_id)
            if (target is not None and name in _MOVE_BLOCKED
                    and (name not in _SAGA_ONLY_BLOCKED or session_id in self._moving)):
                from server.sessions.session_move import SessionMoving
                raise SessionMoving(session_id, target)
            cached = session_id in self._host_cache
            host = await self._host_for(session_id)
            orch = self._orch_for_host_or_primary(host)
            try:
                result = await getattr(orch, name)(session_id, *a, **k)
            except Exception as exc:
                if not (cached and _is_unknown_session_error(exc)):
                    raise
                healed = await self._reroute(session_id, host)
                if healed is None:
                    raise
                result = await getattr(healed, name)(session_id, *a, **k)
            else:
                if result is None and cached and name in _NONE_MEANS_UNKNOWN:
                    healed = await self._reroute(session_id, host)
                    if healed is not None:
                        result = await getattr(healed, name)(session_id, *a, **k)
            if name == "get_session":
                if result is None and target is not None:
                    # Between export and import neither daemon may hold a record (a
                    # worker source forgets it; the target has not loaded it yet).
                    # The session still exists: serve its row through the one mapper.
                    result = await self._record_from_row_of(session_id)
                self._overlay_moving(result)
            return result
        if getattr(getattr(RemoteOrchestrator, name, None), _DEADLINE_ATTR, False):
            setattr(_call, _DEADLINE_ATTR, True)
        return _call

    # ── Move to host (D-17): what server/sessions/session_move.py drives ──────────
    @property
    def primary_name(self) -> str:
        return self._primary

    def host_names(self) -> list[str]:
        return self._router.names()

    async def host_candidates(self):
        return await self._router.candidates()

    def orch_for_host(self, host: str):
        """The orchestrator of fleet host *host*, explicitly — the saga routes export and
        import to DIFFERENT hosts for one session id, so never through ``_host_for``.
        Raises ``UnknownHost``."""
        return self._router.get(host)

    def note_host(self, session_id: str, host: str) -> None:
        """The edge restamped *session_id* to *host*: route there from now on."""
        self._host_cache[session_id] = host

    def begin_move(self, session_id: str, target: str) -> None:
        self._moving[session_id] = target

    def end_move(self, session_id: str) -> None:
        self._moving.pop(session_id, None)

    def moving_target(self, session_id: str) -> str | None:
        """The target of a saga running in THIS edge (the reconcile skips these)."""
        return self._moving.get(session_id)

    def hold_move(self, session_id: str, target: str) -> None:
        self._held[session_id] = target

    def release_hold(self, session_id: str) -> None:
        self._held.pop(session_id, None)

    def held_ids(self) -> list[str]:
        return list(self._held)

    def lease_wait_seconds(self, session_id: str) -> float:
        """Seconds since the reconcile first saw this lease-without-marker state."""
        first = self._lease_wait.setdefault(session_id, time.monotonic())
        return time.monotonic() - first

    def clear_lease_wait(self, session_id: str) -> None:
        self._lease_wait.pop(session_id, None)

    @staticmethod
    async def _record_from_row_of(session_id: str):
        from sqlalchemy import select as _sel
        from server.db import db_session
        from server.db.models import Session as _S
        from server.sessions.orchestrator import SessionOrchestrator
        async with db_session() as db:
            row = (await db.execute(_sel(_S).where(_S.id == session_id))).scalar_one_or_none()
        return SessionOrchestrator._record_from_row(row) if row is not None else None

    def _overlay_moving(self, rec) -> None:
        """Every record this edge serves says whether it is moving right now."""
        if rec is None:
            return
        try:
            rec.moving_to = self._moving.get(rec.id) or self._held.get(rec.id)
        except Exception:  # pragma: no cover — frozen record types
            pass

    async def stamp_started_at(self) -> None:
        """Read ``started_at`` from the DB clock (the one ``move_started_at`` is written
        with). Never raises: on a failed read the local-clock placeholder stays."""
        from server.sessions.session_move import db_now
        try:
            self.started_at = await db_now()
        except Exception:
            log.info("fleet: DB clock read failed; started_at stays the edge's clock",
                     exc_info=True)

    async def move_reconcile_loop(self) -> None:
        """Settle move claims a dead saga left behind (startup, then every minute)."""
        from server.sessions.session_move import reconcile_loop
        await reconcile_loop(self)

    async def create_session(self, *, prefer: str | None = None,
                             platform: str | None = None, **kwargs):
        engine: AgentEngine | None = None
        if platform is not None:
            # Sent only when set: an ungated request stays byte-identical for a
            # daemon that predates the key. A gated one is only ever offered hosts
            # that advertise the platform (pick below), which no such daemon does.
            kwargs["platform"] = platform
        if "engine" in kwargs:
            engine = parse_agent_engine(kwargs["engine"])
            if engine is AgentEngine.CLAUDE_CODE:
                # Preserve the exact legacy request for mixed-version runners: a
                # daemon that predates the key rejects it, and Claude is the default
                # it would have applied anyway.
                kwargs.pop("engine")
                engine = None
            else:
                # Normalize to the enum so every downstream hop sees one type. It is
                # a StrEnum, so the wire encoding is unchanged.
                kwargs["engine"] = engine
        # The assistant is pinned to the primary: its tools call the edge's own loopback
        # API and its in-process MCP server lives in the primary daemon, so a worker
        # placement would have neither. Overrides any caller preference.
        assistant = is_assistant_kind(kwargs.get("kind"))
        if assistant:
            prefer = self._primary
        # A client sitting at a fleet host's own desktop should land there, or "route
        # to the native display" can never hold. An explicit host still wins, and an
        # explicit "virtual" request has no reason to move.
        if prefer is None and kwargs.get("display") != DISPLAY_VIRTUAL:
            # The tailnet node name and the runner-registry name are one host spelled
            # by two independent sources, so match case-insensitively — the same
            # comparison resolve_display_mode makes, or a "Dobby" client would be
            # routed as native by the policy and placed elsewhere by the router. The
            # registry's OWN spelling becomes `prefer`: it is the key every downstream
            # FleetRouter.get() lookup uses.
            _cn = (kwargs.get("client_node") or "").strip().lower()
            if _cn:
                prefer = next(
                    (n for n in self._router.names() if n.strip().lower() == _cn), None)
        # Candidate selection now filters on the requested engine before scoring, so a
        # Codex launch is only ever offered runners that advertised a usable Codex
        # boundary. Raises NoEngineHostAvailable (a NoHostAvailable) when every healthy
        # host is incompatible, carrying the per-host reason.
        # `ignore_capacity` only for the assistant: it neither counts against nor
        # queues behind the session cap anywhere else (the daemon's global-cap gate,
        # `_consumes_capacity_slot`, `owned_active_session_count`), so ranking it
        # against `active_sessions >= max_sessions` refused a slotless session for
        # lack of a slot and left the drawer unopenable on a full primary (#2184).
        sha = expected_contract_sha256(engine)
        try:
            order = await self._router.pick(
                prefer=prefer, engine=engine, platform=platform,
                expected_contract_sha256=sha, ignore_capacity=assistant)
        except NoEngineHostAvailable:
            # "Install the Codex binary" stays the answer: a queued session would
            # wait forever for a runner that does not exist.
            raise
        except NoHostAvailable as refusal:
            if assistant:
                # Capacity was already ignored for it, so this refusal is a health
                # problem — and an assistant parked in a queue is a drawer that
                # never opens.
                raise
            # Every host is enabled, healthy and FULL (dobby 5/4, rocky 8/6). That is
            # a queueing condition, not a refusal: the daemon already persists a
            # 'queued' row for the fleet-wide cap, so ask it to do exactly that. The
            # split-brain half of the refusal is kept — nothing runs in-process here;
            # the row is written by a daemon, and started by whichever daemon frees a
            # slot (see SessionOrchestrator._maybe_start_queued's host-capacity rule).
            return await self._queue_for_capacity(refusal, engine=engine, platform=platform,
                                                  sha=sha, kwargs=kwargs)
        if assistant:
            # `prefer` only REORDERS — pick() keeps the other hosts as failover, and
            # _dispatch cascades past a primary whose spawn raised. For the assistant
            # either leak is worse than a refusal: a worker has no edge on its
            # loopback, so the thread is created fine and then EVERY tool call fails
            # with ConnectError (report_gap alone, DB-only, still works), and the
            # drawer reuses that thread indefinitely (2026-10-09: ten such threads on
            # rocky since 09-07). Dispatch to the primary alone, or refuse.
            if self._primary not in order:
                raise NoHostAvailable(
                    f"the assistant runs only on the primary host {self._primary!r}, "
                    "which is not dispatchable right now; retry once it is healthy")
            order = [self._primary]
        return await self._dispatch(order, kwargs)

    async def _dispatch(self, order: list[str], kwargs: dict):
        """Try *order* best-first; the first daemon that accepts wins."""
        last_exc: Exception | None = None
        for host in order:
            try:
                rec = await self._router.get(host).create_session(**kwargs)
            except DaemonUnavailable as e:
                if e.outcome_unknown:
                    # The RPC reached the host and timed out or reset mid-response: the
                    # daemon may still be spawning — or may already have persisted the
                    # session (2026-09-07: it landed 2s after the edge gave up). Trying
                    # the next host would start a SECOND session for the same prompt on
                    # another runner (the cross-host duplicate pairs of 2026-09-05).
                    # Propagate; the edge tells the user the session may still appear.
                    log.warning("fleet.create_session host=%s outcome unknown (%s) — "
                                "not cascading to the next host", host, e)
                    raise
                last_exc = e  # never reached the daemon → safe to try the next host
                continue
            except Exception as e:  # spawn/clone/account errors → try next ranked host
                last_exc = e
                continue
            if kwargs.get("ownership_token") is not None:
                # The selected daemon may have JOINED an earlier reserved launch
                # on another host. Its persisted host is authoritative; stamping
                # this RPC's destination would route later prompts to the wrong daemon.
                self._host_cache[rec.id] = rec.host or self._primary
                return rec
            self._host_cache[rec.id] = host
            await self._stamp_host(rec.id, host)
            try:
                rec.host = host
            except Exception:  # pragma: no cover — frozen record types
                pass
            return rec
        raise last_exc if last_exc is not None else NoHostAvailable("no host accepted the session")

    async def _queue_hosts(self, *, engine: AgentEngine | None, platform: str | None,
                           sha: str | None) -> list[str]:
        """Daemons that can PERSIST a queued row, best first — the primary leads.

        Preferring the primary keeps the row on the daemon that owns unstamped and
        orphaned queue entries anyway, and whose edge is co-located. Hosts that are
        up but full come first (``pick`` with the capacity leg dropped — the same
        exemption the assistant uses); when none is dispatchable at all, any host the
        registry does not know to be unreachable is still worth asking: a
        skew-fenced daemon cannot START a session but can perfectly well persist one,
        and a durable queued row beats an error.

        A *platform*-gated session gets no such fallback: it may only be persisted
        on a host advertising the platform, or a Linux daemon would own the row.
        With none, the refusal stands (the daemon drain applies the same rule,
        ``SessionOrchestrator._local_host_serves_platform``).
        """
        try:
            hosts = await self._router.pick(
                engine=engine, platform=platform, expected_contract_sha256=sha,
                ignore_capacity=True)
        except NoHostAvailable:
            if platform is not None:
                return []
            hosts = await self._fanout_names()
        return sorted(hosts, key=lambda h: (h != self._primary, h))

    async def _queue_for_capacity(self, refusal: NoHostAvailable, *,
                                  engine: AgentEngine | None, platform: str | None,
                                  sha: str | None, kwargs: dict):
        """Persist the session as 'queued' on the first daemon that will take it.

        Re-raises *refusal* (the typed NoHostAvailable the API already maps to its
        409) only when no daemon could be reached at all — a transport error from a
        queue attempt must not masquerade as the capacity verdict, and an older
        daemon that rejects the ``queue_only`` kwarg is simply skipped.
        """
        hosts = await self._queue_hosts(engine=engine, platform=platform, sha=sha)
        if not hosts:
            raise refusal
        log.info("fleet.create_session: every host is at capacity — queueing on %s",
                 hosts[0])
        try:
            rec = await self._dispatch(hosts, {**kwargs, "queue_only": True})
        except DaemonUnavailable as e:
            if e.outcome_unknown:
                raise
            log.warning("fleet.create_session: no daemon could persist the queued "
                        "session (%s) — refusing", e)
            raise refusal from e
        except Exception as e:  # noqa: BLE001 — any failure to queue is the refusal
            log.warning("fleet.create_session: queueing failed (%s) — refusing", e)
            raise refusal from e
        log.info("fleet.create_session queued id=%s host=%s reason=host_capacity",
                 rec.id, getattr(rec, "host", None))
        return rec

    async def _queued_count(self) -> int:
        """How many sessions sit in the shared queue right now (a plain DB read)."""
        from server.sessions import session_queue as sq
        return len(await sq.positions())

    async def drain_queue_once(self) -> list[str]:
        """Ask every host with headroom to drain the queue; returns who was asked.

        The daemons' own drains are event-driven and host-local: a slot freed on
        rocky fires rocky's drain, which can adopt a session queued on a full dobby
        — but only once the registry (refreshed by the 30 s probe) shows dobby full.
        If rocky's drain ran before that, nothing on rocky fires again until its
        next slot-free event, and the queued session waits for no reason. This
        nudge closes that gap on the probe cadence. Every call is idempotent on the
        daemon side (lock-serialized, lease-fenced), so asking too often is harmless.

        Hosts WITHOUT headroom are not asked: their drain would stop at its own
        cap anyway. An unreachable host is skipped quietly — the probe is the
        detector that reports it, and a nudge that fails costs nothing.
        """
        if await self._queued_count() == 0:
            return []
        try:
            hosts = await self._router.pick()
        except NoHostAvailable:
            return []
        asked: list[str] = []
        for host in hosts:
            try:
                started = await self._router.get(host).maybe_start_queued()
            except Exception as e:  # noqa: BLE001 — a dead host must not stop the rest
                log.debug("fleet.queue_drain host=%s nudge failed: %s", host, e)
                continue
            asked.append(host)
            if started:
                log.info("fleet.queue_drain host=%s started %d queued session(s)",
                         host, started)
        return asked

    async def automation_trigger(self, automation_id: str, item_id: str, kind: str,
                                 identity: str | None = None) -> dict:
        """Serve an Archive Audit ``reopen`` on the edge (#3142); delegate the rest.

        The finding's session may live on a worker, and only the edge holds the mTLS
        client identity workers accept, so the decision runs here with the edge's own
        per-session ``unarchive`` routing instead of in the primary daemon."""
        if automation_id == "archive-audit" and kind == "reopen":
            from server.automations.action_providers import ArchiveAuditActionProvider
            from server.automations.archive_audit import decide_finding
            from server.db import db_session

            fleet = self

            class _EdgeDecider:
                async def archive_audit_decide_finding(self, fid: str, decision: str) -> str:
                    return await decide_finding(db_session, fid, decision,
                                                fleet._edge_unarchive)

            return (await ArchiveAuditActionProvider(_EdgeDecider()).trigger(
                item_id, kind)).to_dict()
        return await self._primary_orch().automation_trigger(
            automation_id, item_id, kind, identity=identity)

    async def _edge_unarchive(self, session_id: str):
        """Per-session-routed unarchive for an operator's reopen: a failure is a toast,
        not a 500 — an unreachable host is routine, anything else is worth a WARNING."""
        try:
            return await self.unarchive(session_id)
        except DaemonUnavailable as exc:
            log.info("archive_audit reopen: host unreachable for %s: %s", session_id, exc)
        except Exception:
            log.warning("archive_audit reopen failed for %s", session_id, exc_info=True)
        return None

    async def archive_reopen_once(self) -> dict:
        """Finish the auto-reopens the primary daemon handed to the edge (#3142)."""
        from server.automations.archive_audit import run_edge_unarchive_pass
        from server.db import db_session
        return await run_edge_unarchive_pass(db_session, self.unarchive)

    async def archive_reopen_loop(self,
                                  interval_s: float = ARCHIVE_REOPEN_INTERVAL_S) -> None:
        """Run :meth:`archive_reopen_once` forever; a raising tick is logged, not fatal."""
        while True:
            try:
                await self.archive_reopen_once()
            except Exception:
                log.warning("fleet.archive_reopen_tick_failed", exc_info=True)
            await asyncio.sleep(interval_s)

    async def queue_drain_loop(self, interval_s: float = QUEUE_DRAIN_INTERVAL_S) -> None:
        """Nudge forever on the probe cadence; a raising tick is swallowed."""
        while True:
            try:
                await self.drain_queue_once()
            except Exception:
                log.exception("fleet.queue_drain_tick_failed")
            await asyncio.sleep(interval_s)

    async def _stamp_host(self, session_id: str, host: str) -> None:
        from sqlalchemy import update as _upd
        from server.db import db_session
        from server.db.models import Session as _S
        async with db_session() as db:
            await db.execute(_upd(_S).where(_S.id == session_id).values(host=host))
            await db.commit()

    async def _fanout_names(self) -> list[str]:
        """Hosts to fan an aggregate read out to — the ONE answer all four sites use.

        Delegates to the router, which drops hosts the registry knows are unreachable
        (see FleetRouter.reachable_names). Before this, every fan-out asked
        ``_router.names()`` — every registered host, including one that had been
        powered off for 17 hours — and paid its connect timeout plus retry on every
        request: 13.04s on /api/sessions, which the UI polls every 5s (2026-09-09).

        Degrades to ``names()`` if the router cannot answer, so a registry problem or a
        router without the method makes the fan-out slow again, never blind.
        """
        fn = getattr(self._router, "reachable_names", None)
        if fn is None:
            return self._router.names()
        try:
            return await fn(always=self._primary)
        except Exception:
            log.warning("fleet: reachable_names failed — fanning out to every host",
                        exc_info=True)
            return self._router.names()

    async def _autopilot_fleet_status(self, primary_status: dict | None) -> dict:
        from server.automations.autopilot_observation import merge_autopilot_statuses

        observations: dict[str, dict | None] = dict.fromkeys(self._router.names())
        observations[self._primary] = primary_status

        async def read_worker(host: str) -> None:
            try:
                async with asyncio.timeout(AUTOPILOT_STATUS_TIMEOUT_S):
                    statuses = await self._router.get(host).automation_statuses()
            except Exception as exc:
                await self._log_unreachable(host, exc, "automation_statuses")
                return
            self._note_reachable(host)
            observations[host] = statuses.get("autopilot") if isinstance(statuses, dict) else None

        await asyncio.gather(*(read_worker(host) for host in await self._fanout_names()
                               if host != self._primary))
        return merge_autopilot_statuses(observations, now=time.time())

    async def automation_statuses(self) -> dict[str, dict]:
        """Only host-owned autopilot observations need fleet aggregation."""
        statuses = await self._primary_orch().automation_statuses()
        self._note_reachable(self._primary)
        return {**statuses, "autopilot": await self._autopilot_fleet_status(
            statuses.get("autopilot"))}

    async def automation_overview(self) -> dict:
        """Keep all actionable counts primary-owned; add worker autopilot evidence."""
        overview = await self._primary_orch().automation_overview()
        self._note_reachable(self._primary)
        statuses = overview.get("statuses", {})
        return {**overview, "statuses": {
            **statuses, "autopilot": await self._autopilot_fleet_status(statuses.get("autopilot"))}}

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
                    *(_one(n) for n in await self._fanout_names())):
                if exc is not None:
                    if host == self._primary:
                        raise exc
                    await self._log_unreachable(host, exc, name)
                    continue
                self._note_reachable(host)
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
        names = await self._fanout_names()
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
                await self._log_unreachable(name, exc, "list_sessions")
                continue
            self._note_reachable(name)
            for rec in recs or []:
                if not include_assistant and is_assistant(rec):
                    continue
                stamp = getattr(rec, "host", None)
                if (name != self._primary and stamp is not None and stamp != name
                        and getattr(rec, "detached", False)):
                    # A worker serving a DRIVERLESS copy stamped for another host is
                    # holding a stale record (another host adopted its queue entry),
                    # not running the session. Letting it win the merge pinned every
                    # per-session action to that worker, which answered "not in the
                    # queue" (4a558770, 2026-10-09).
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
            cached = self._host_cache.get(sid)
            if cached is not None and cached != self._primary and cached in answered:
                # A worker once served this id and has since stopped: forget the
                # route so _host_for re-reads the row's stamp instead of pinning
                # the session to a host that no longer holds it.
                self._host_cache.pop(sid, None)
            rec = merged[sid]
            owner = getattr(rec, "host", None)
            if (owner is not None and owner != self._primary and owner in answered
                    and getattr(rec, "detached", False)):
                log.info("fleet.list_sessions dropped stale primary mirror "
                         "session_id=%s owner=%s (owner answered without it)", sid, owner)
                del merged[sid]
        for rec in merged.values():
            self._overlay_moving(rec)
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
            *(_one(n) for n in await self._fanout_names())
        ):
            if exc is None:
                invalidated.append(name)
            else:
                failed[name] = str(exc)
                log.error("fleet.codex_invalidate_credentials host=%s account=%s "
                          "FAILED — the account may still be usable there",
                          name, account, exc_info=exc)
        return {"invalidated": invalidated, "failed": failed}

    async def plugin_op_everywhere(self, op: str, args: dict) -> list[dict]:
        """Run one plugin op on EVERY registered host and name each outcome (D-16c, D2).

        Not ``_MERGE_ALL_HOSTS``, for the same reason as ``codex_invalidate_credentials``:
        that helper drops an unreachable worker, and an install that silently skipped a
        host is the one outcome the owner must see. A host the registry already knows is
        down is reported ``host_unreachable`` without being dialled; the rest run
        concurrently. Nothing is rolled back — a retry is idempotent (a no-op is ``noop``).
        """
        from server.library.plugin_cli import host_outcome, unreachable
        reachable = set(await self._fanout_names())

        async def _one(name: str) -> dict:
            if name not in reachable:
                return unreachable(name)
            return await host_outcome(name, self._router.get(name).plugin_op(op, args))

        return list(await asyncio.gather(*(_one(n) for n in self._router.names())))

    async def reload_plugins_everywhere(self, hold_on_cache_impact: bool = True) -> list[dict]:
        """Live-reload plugins in every session on EVERY host (D-16c residuals).

        Same fan-out contract as ``plugin_op_everywhere``: a host the registry knows is down
        is reported without being dialled, and a host that fails yields exactly one
        ``failed`` row (no ``sessionId``) rather than an exception. The result is the
        concatenation of every reachable host's per-session rows.
        """
        from server.library.plugin_cli import unreachable
        from server.sessions.plugin_reload import host_rows
        reachable = set(await self._fanout_names())

        async def _one(name: str) -> list[dict]:
            if name not in reachable:
                return [unreachable(name)]
            return await host_rows(name, self._router.get(name).reload_plugins_live(
                hold_on_cache_impact=hold_on_cache_impact))

        return [row for rows in await asyncio.gather(*(_one(n) for n in self._router.names()))
                for row in rows]

    async def push_credentials(self, lease_s: float = CREDS_LEASE_S) -> dict[str, dict[str, int]]:
        """Mint the primary's Claude envelopes and push them to every runner (#2629), carrying
        the shared MCP store's public view and purge keys with them (#3287).

        Returns ``{host: acks}``. A runner that is unreachable is skipped (its lease lapses on
        its own — that is the fail-closed path); one that acks a lower generation than was pushed
        is reported as a fleet warning, never an error.

        The MCP view is the store read once per tick, every dict bundle reduced to
        ``creds.public_view`` (no refresh token leaves the edge), plus ``grants.purge_keys``;
        a store that cannot be read costs this tick its MCP fields only (the runner keeps its
        cached view), never the envelope lease. A runner answering with ``mcp_offers`` (its
        terminal logins) has them merged into the store under the ordinary policy; a merge
        failure is a WARNING that leaves the acks untouched.
        """
        from viberpit import accounts as _accounts
        from viberpit import creds
        from server.mcp_health import grants
        from server.sessions.claude_authority import mint_envelopes

        envs = await asyncio.to_thread(mint_envelopes, _accounts._anthropic_account_names())
        wire = [{"account": e.account, "kind": e.kind, "token": e.token,
                 "expires_at": e.expires_at, "generation": e.generation} for e in envs]
        want = {e.account: e.generation for e in envs}
        mcp: dict = {}  # the MCP fields, absent (not None) when the store could not be read
        try:
            store = await asyncio.to_thread(creds.read_shared_mcp)
            mcp = {"mcp_bundles": {k: creds.public_view(v) for k, v in store.items()
                                   if isinstance(v, dict)},
                   "mcp_drop": sorted(grants.purge_keys(store))}
        except Exception:  # noqa: BLE001 — the MCP view must never cost a runner its lease
            log.warning("fleet.creds_sync mcp view unreadable; pushing envelopes only", exc_info=True)

        async def _one(name: str):
            try:
                res = await self._router.get(name).creds_sync(wire, lease_s, **mcp)
                return name, res, None
            except Exception as e:  # noqa: BLE001 — one runner must not block the rest
                return name, None, e

        out: dict[str, dict[str, int]] = {}
        targets = [n for n in await self._fanout_names() if n != self._primary]
        for name, res, exc in await asyncio.gather(*(_one(n) for n in targets)):
            if exc is not None:
                await self._log_creds_push_failure(name, exc)
                continue
            self._creds_unreachable.pop(name, None)
            acks = res.get("acks", {})
            out[name] = acks
            behind = sorted(a for a, g in want.items() if acks.get(a, -1) < g)
            if behind:
                log.warning("fleet.creds_sync host=%s behind on accounts=%s", name, behind)
            offers = res.get("mcp_offers")
            if isinstance(offers, dict) and offers:
                await self._merge_runner_offers(name, offers)
        return out

    @staticmethod
    async def _merge_runner_offers(name: str, offers: dict) -> None:
        """Merge a runner's offered MCP bundles into the shared store (#3287): the edge is the
        single writer, so a terminal login made on a runner lands here under the store lock
        with the same newer-wins policy as the edge's own dir capture. Logs keys, never
        token values; a failure is a WARNING."""
        from viberpit import creds

        log.info("fleet.creds_sync host=%s offered mcp bundle(s) keys=%s", name, sorted(offers))
        try:
            await asyncio.to_thread(creds.merge_shared_mcp, offers)
        except Exception:  # noqa: BLE001 — a merge failure must not affect the acks
            log.warning("fleet.creds_sync host=%s mcp offer merge failed keys=%s",
                        name, sorted(offers), exc_info=True)

    async def _log_creds_push_failure(self, name: str, exc: BaseException) -> None:
        """Log a failed push at the level its lease risk earns (#2757).

        ``DaemonUnavailable`` (and its ``DaemonTimeout`` subclass) extends the host's run of
        misses: INFO below ``CREDS_UNREACHABLE_ESCALATE_AFTER`` or while a deploy restart is
        expected, WARNING from then on. Any other error comes from a daemon that answered, so
        it ends the run and is a WARNING at once.
        """
        if not isinstance(exc, DaemonUnavailable):
            self._creds_unreachable.pop(name, None)
            log.warning("fleet.creds_sync host=%s failed: %s", name, _describe_push_error(exc))
            return
        run = self._creds_unreachable.get(name, 0) + 1
        self._creds_unreachable[name] = run
        quiet = (run < CREDS_UNREACHABLE_ESCALATE_AFTER
                 or await host_upgrade.restart_expected(name))
        log.log(logging.INFO if quiet else logging.WARNING,
                "fleet.creds_sync host=%s unreachable (consecutive=%d): %s",
                name, run, _describe_push_error(exc))

    async def creds_push_loop(self, interval_s: float = CREDS_PUSH_INTERVAL_S) -> None:
        """Push envelopes forever on the fleet probe cadence; a raising tick is swallowed."""
        while True:
            try:
                await self.push_credentials()
            except Exception:
                log.exception("fleet.creds_push_tick_failed")
            await asyncio.sleep(interval_s)

    async def codex_account_status(self, account: str, *, force: bool = False) -> dict:
        """Merge every runner's status, then persist once from the edge.

        The merged reading is still returned degraded when no host could read
        quota, but ``persist_quota_snapshot`` refuses to write it: an edge that
        re-persisted "unknown" on every accounts-page poll kept the ledger
        entry perpetually fresh, so no runner's refresher ever re-read the
        app-server and the account stayed "Usage unavailable" for good.
        """

        async def _one(host: str):
            try:
                result = await self._router.get(host).codex_account_status(
                    account, persist=False, force=force
                )
                return host, result, None
            except Exception as exc:  # noqa: BLE001 -- isolate worker read failure
                return host, None, exc

        parts: list[dict] = []
        for host, result, exc in await asyncio.gather(
            *(_one(name) for name in await self._fanout_names())
        ):
            if exc is not None:
                if host == self._primary:
                    raise exc
                await self._log_unreachable(host, exc, "codex_account_status")
                continue
            self._note_reachable(host)
            parts.append(result or {})
        merged = _merge_codex_status(parts)
        raw_quota = merged.get("quota")
        if isinstance(raw_quota, dict):
            snapshot = QuotaSnapshot.from_dict(raw_quota)
            await asyncio.to_thread(persist_quota_snapshot, account, snapshot)
        return merged

    async def pr_detail(self, repo: str, number: int) -> "dict | None":
        """PR tab detail: the primary's answer, else the first worker that has it.

        ``_session_pr_entry`` reads only the serving daemon's own sessions, so a PR
        owned by a worker-host session and outside the Pipeline population is a miss
        on the primary ("Live details unavailable"). A primary ``pr_not_synced``
        marker is kept when no worker has the PR either, so the route still 502s
        with the reason rather than 404ing."""
        primary = await self._primary_orch().pr_detail(repo, number)
        if primary is not None and not primary.get("pr_not_synced"):
            return primary

        async def _one(name: str):
            try:
                return await self._router.get(name).pr_detail(repo, number)
            except Exception as e:  # noqa: BLE001 — a dead worker must not blank the tab
                await self._log_unreachable(name, e, "pr_detail")
                return None

        workers = [n for n in await self._fanout_names() if n != self._primary]
        for found in await asyncio.gather(*(_one(n) for n in workers)):
            if found is not None and not found.get("pr_not_synced"):
                return found
        return primary

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
