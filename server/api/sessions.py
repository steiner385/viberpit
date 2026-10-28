"""Sessions REST API router (T036).

Routes:
  GET  /api/sessions               → list sessions
  POST /api/sessions               → create session (201) or 409 if all-busy
  GET  /api/sessions/{id}          → get single session (404 if unknown)
  PATCH /api/sessions/{id}         → rename session (200)
  DELETE /api/sessions/{id}        → close session (204)

All routes require a verified identity via the ``current_identity`` dependency.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import os
import re
from datetime import datetime
from typing import Any, Literal, TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from viberpit import accounts as _accounts
from viberpit import gitrepos
from server.auth.identity import human_gate_via
from server.auth.middleware import current_client_node, current_identity
from server.library.names import valid_mcp_name
from server.mcp_scope.servers import configured_server_keys
from server.quick_replies import observe_reply
from server.automations.registry import assisted_by_label, origin_label, work_class_for_origin
from server.sessions.search import parse_terms, session_matches
from server.sessions.types import (
    ASSISTANT_KIND, AUTO_ARCHIVE_OPT_OUT_CREATE, RESUME_ELIGIBLE_STATUSES, is_assistant,
)
from server.sessions.usage_stall import is_usage_stalled
from server.audit import record_audit
from server.config import get_settings
from server.db import db_session, get_db
from server.sessions.orchestrator import (
    ArchiveMissingError,
    ArchiveOwnedElsewhereError,
    BranchSwitchError,
    InsufficientStorageError,
    ModelBlockedError,
)
from server.sessions import session_actions
from server.sessions.session_actions import ActionError
from server.ipc.client import DaemonError, DaemonTimeout, DaemonUnavailable
from server.sessions.facade import call_with_deadline
from server.sessions.engine import AgentEngine, AgentEngineUnavailable, parse_agent_engine
from server.sessions.model_policy import quota_model
from server.sessions.permissions import resolve_permission_mode
from server.sessions.selection import AllAccountsBusy, select_account
from server.api.fleet_errors import FleetDispatchRefused, fleet_refusal_response
from server.sessions.background_tasks import EMPTY_BACKGROUND_WORK
from server.obs.logdedupe import LogThrottle, log_event_throttled
from server.sessions import host_heartbeat
from server.time_iso import iso_utc

if TYPE_CHECKING:
    from server.sessions.facade import InProcessOrchestratorAdapter

# Repo nameWithOwner shape: owner/name — each segment alphanumeric + . _ -
# No leading/trailing dots, no consecutive dots (blocks path-traversal via ../foo).
_REPO_SEGMENT = r"(?!\.{2,})[A-Za-z0-9][A-Za-z0-9._-]*"
_REPO_RE = re.compile(rf"^{_REPO_SEGMENT}/{_REPO_SEGMENT}$")

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


def _require_adoption_enabled() -> None:
    """Sunset gate: adoption is off unless VIBERPIT_ENABLE_ADOPTION is set."""
    if not get_settings().enable_adoption:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class CreateSessionRequest(BaseModel):
    name: str | None = None
    account: str | None = None
    initialPrompt: str | None = None
    repo: str | None = None  # nameWithOwner, e.g. "owner/name"
    branch: str | None = None
    permissionMode: str | None = None
    attachmentIds: list[str] | None = None
    # Model alias/id to start the session on (e.g. "opus", "fable"). None / "default"
    # → the account default; the value is applied to the driver once it starts.
    model: str | None = None
    engine: AgentEngine = AgentEngine.CLAUDE_CODE
    reasoningEffort: str | None = None
    # Optional runner preference. None → auto-rank; a name → that host first, still
    # failing over. Forwarded to FleetOrchestrator.create_session as prefer=.
    host: str | None = None
    # Where the session's GUI renders. None/"auto" → decided from the requesting
    # client's locality (native when the browser is on the runner host); "native" /
    # "virtual" force it. Only the resolved mode is persisted (Session.displayMode).
    display: Literal["auto", "native", "virtual"] | None = None
    presetId: str | None = None
    permissionProfileId: str | None = None
    # Per-session opt-outs. None / True = governed by the global autopilot mode and
    # the app-wide auto-archive setting (the pre-toggle behaviour); False = this
    # session only. They land on the EXISTING `autopilot_paused` /
    # `auto_archive_opt_out_reason` columns (see the handler) — no parallel
    # "enabled" columns, so the governance and completion gates need no new branch.
    autopilot: bool | None = None
    autoArchive: bool | None = None


class RenameSessionRequest(BaseModel):
    name: str


class AutopilotPauseRequest(BaseModel):
    paused: bool


class AutoArchiveOptOutRequest(BaseModel):
    optedOut: bool


class SuspectedCompleteRequest(BaseModel):
    """The user's answer to a "likely complete" hand-back: ``confirm`` archives the
    session (the objective was met), ``reject`` clears the overlay (it was not)."""
    decision: Literal["confirm", "reject"]


class QueueMoveRequest(BaseModel):
    op: Literal["up", "down", "top", "position"]
    position: int | None = None


class AdoptRequest(BaseModel):
    claudeSessionId: str


class SendPromptRequest(BaseModel):
    text: str
    replySource: Literal["typed", "suggested"] = "typed"
    # "Interrupt and post" (same contract as the WS `prompt` frame, ws.py ~1080):
    # stop the in-flight turn and run this prompt next instead of queueing it.
    interrupt: bool = False


class ModelApprovalBody(BaseModel):
    decision: Literal["approve", "deny"]
    # Optional counter-offer: approve the hold at THIS model instead of the parked
    # one. Absent = approve exactly what was requested (pre-override behaviour).
    model: str | None = None


class FixPrRequest(BaseModel):
    number: int


_UNSET = object()

# Passed as ``heartbeats`` when the per-request heartbeat snapshot could not be read
# (DB down at the edge). Distinct from ``None`` (mutation endpoints that never fetch a
# snapshot — those OMIT liveness) and from an empty dict (snapshot succeeded, no rows —
# ``fresh``): a failed snapshot yields ``unknown`` so degradation is visible, never
# false-fresh (spec 002 NFR-5).
_SNAPSHOT_FAILED = object()


def _record_to_dict(
    record, repo: Any = _UNSET, suggestions: list[str] | None = None,
    prs: list | None = None, summary: str = "", defer_seconds: int | None = None,
    queue_position: int | None = None, queue_stall_reason: dict[str, Any] | None = None,
    heartbeats: Any = None, heartbeats_now: datetime | None = None,
) -> dict[str, Any]:
    """Convert an in-memory SessionRecord to the wire-format dict.

    ``repo`` lets callers inject a value read live from the session's working
    directory (see :func:`_resolve_repo`); when omitted, the stored
    ``record.repo`` is used as a fallback.

    ``suggestions`` holds the latest cached next-prompt suggestions for the
    session (from ``orch.latest_suggestions``). Defaults to [] when omitted
    (detached sessions or none yet generated).

    ``prs`` is the list of cached PullRequestInfo for the session's authored
    branches.  Serialised to a list of dicts on the wire.

    ``summary`` is the one-line status summary for the session (from
    ``orch.latest_summary``). Defaults to "" when omitted or unavailable.

    ``queue_stall_reason`` is set (only on queued records) when the queue is
    non-empty and cannot drain, or the session has waited past
    ``QUEUE_WAIT_GRACE_S`` — see :func:`_queue_stall_reasons`.
    ``None`` for every non-queued record and for a queued record whose queue
    is not actually stalled. Purely additive: a client that has never heard of
    this field sees nothing new to worry about.
    """
    from datetime import timedelta, timezone
    from server.sessions.goals import effective_goal
    from server.sessions.liveness import derive_liveness, heartbeat_interval_s, host_dead_after_s
    # Per-session liveness is DERIVED at the edge from the owning host's heartbeat
    # (spec 002 FR-102/FR-103), never stored. `heartbeats` is snapshotted once per
    # request by the caller — never fetched per session (NFR-4) — and arrives as one
    # of three shapes, each a distinct liveness contract:
    #   * None                — a mutation endpoint that never fetched a snapshot: OMIT
    #                           the liveness keys entirely so a field-level cache merge
    #                           cannot clobber an existing demotion with a stale "fresh".
    #   * _SNAPSHOT_FAILED     — the snapshot read raised (DB down at the edge): report
    #                           "unknown" — degradation is visible, never false-fresh (NFR-5).
    #   * dict (may be empty)  — a successful snapshot: derive per host. host=None
    #                           (primary/in-process) and a host with no row are BOTH fresh
    #                           (NFR-3: absence of a row is never evidence of death).
    #
    # Thresholds are evaluated against `heartbeats_now` — the DB clock the snapshot was
    # read at (FR-102), so a host whose local clock lags the edge is not falsely demoted.
    # They come from the shared env_float-backed accessors: this runs inside the serializer
    # for EVERY session with no try/except above it, so a raw float() on a typo'd knob
    # would 500 the entire sessions page.
    _liveness = None
    if heartbeats is _SNAPSHOT_FAILED:
        _liveness = derive_liveness(None, datetime.utcnow(), snapshot_failed=True)
    elif heartbeats is not None:
        _hb_host = getattr(record, "host", None)
        _hb = heartbeats.get(_hb_host) if _hb_host is not None else None
        _liveness = derive_liveness(
            _hb.beat_at if _hb is not None else None,
            heartbeats_now or datetime.utcnow(),
            interval_s=heartbeat_interval_s(),
            dead_after_s=host_dead_after_s(),
        )
    goal_text, goal_source = effective_goal(record)
    cooldown_until = None
    _last = getattr(record, "last_user_interaction_at", None)
    if defer_seconds and _last is not None and not getattr(record, "autopilot_paused", False):
        if _last.tzinfo is not None:                      # aware after an RPC round trip (daemon mode)
            _last = _last.astimezone(timezone.utc).replace(tzinfo=None)
        _until = _last + timedelta(seconds=defer_seconds)
        if _until > datetime.utcnow():
            cooldown_until = iso_utc(_until)
    d: dict[str, Any] = {
        "id": record.id,
        "name": record.name,
        "accountName": record.account,
        "engine": record.engine.value,
        "engineSessionId": record.engine_session_id or None,
        "reasoningEffort": record.reasoning_effort,
        "repo": record.repo if repo is _UNSET else repo,
        # Fleet host that ran this session. None for pre-fleet sessions and for
        # deployments whose endpoint is not in an operator-managed registry.
        "host": getattr(record, "host", None),
        # "native" | "virtual" | null — drives the desktop badge and whether the
        # Browser tab offers a VNC canvas at all.
        "displayMode": getattr(record, "display_mode", None),
        "displayFallbackReason": getattr(record, "display_fallback_reason", None),
        "origin": getattr(record, "origin", None),
        "originRef": getattr(record, "origin_ref", None),
        "assistedBy": getattr(record, "assisted_by", None),
        # Lineage: the session whose dispatch_session call created this one (child-session
        # spec §4.6). null for a root. Same two-serializer rule as `origin`.
        "parentSessionId": getattr(record, "parent_session_id", None),
        # Derived at the edge from the automation registry, so the browser
        # never keeps its own id→name map or its own list of which origins
        # are housekeeping. "development" for a human session or an id the
        # registry does not know (fail visible — see `work_class_of`).
        "workClass": work_class_for_origin(getattr(record, "origin", None)),
        "originLabel": origin_label(getattr(record, "origin", None)),
        "assistedByLabel": assisted_by_label(getattr(record, "assisted_by", None)),
        "status": record.status,
        "nameIsAuto": getattr(record, "name_is_auto", True),
        "detached": record.detached,
        "adoptedInPlace": getattr(record, "adopted_in_place", False),
        "subAgents": getattr(record, "sub_agents", None) or [],
        "backgroundWork": (
            EMPTY_BACKGROUND_WORK
            if record.status == "ended"
            else getattr(record, "background_work", None) or EMPTY_BACKGROUND_WORK
        ),
        "createdAt": iso_utc(record.created_at),
        # Last-turn-response time for the "active … ago" label (NOT record.updated_at,
        # which any row write bumps — that pinned every card to "0s ago"). Fall back to
        # the creation time so a session with no turn yet reads "active just now" rather
        # than null (which the client would parse as epoch 0 → "20627d ago"). iso_utc
        # tags the naive UTC column with a +00:00 offset so the browser doesn't reparse
        # it as local time (which lands it in the future → clamped back to "0s ago").
        "updatedAt": record.updated_at_iso,
        "revAt": record.rev_at,
        "accountPinned": getattr(record, "account_pinned", False),
        # Why a needs_attention/limited session is parked, when it may recover, and
        # the derived "stalled on usage" flag the Rotate-stalled batch button counts.
        # Same two-serializer rule as `resumable`: SessionRecord.to_dict is the
        # daemon→edge RPC copy, this is the browser copy — both must emit them.
        "pausedReason": getattr(record, "paused_reason", None),
        "pausedUntil": iso_utc(getattr(record, "paused_until", None)),
        "usageStalled": is_usage_stalled(record.status, getattr(record, "paused_reason", None)),
        "model": getattr(record, "model", None),
        # Approval-hold model: set when the model-approval gate parks a requested
        # model pending admin sign-off (status="pending_approval" or a mid-session
        # hold). Null once approved/denied or for non-gated sessions.
        "pendingModel": getattr(record, "pending_model", None),
        # Why the session asked for pendingModel (its own stated reason), or null.
        "pendingModelReason": getattr(record, "pending_model_reason", None),
        # Identity that requested pendingModel; "session:self" when the session asked
        # for itself (vs. a human requesting on its behalf).
        "pendingModelRequestedBy": getattr(record, "pending_model_requested_by", None),
        # True while a "switch after this turn" model change waits for the in-flight
        # turn to end (transient, never persisted) — the picker shows it as pending.
        "modelPending": getattr(record, "model_pending", False),
        "latestSuggestions": list(suggestions or []),
        "activeGoal": getattr(record, "active_goal", None),
        "inferredGoal": getattr(record, "inferred_goal", None),
        # The reckoned goal autopilot drives toward, resolved through the single
        # effective_goal() helper (explicit /goal wins, else the title fallback).
        "effectiveGoal": goal_text,
        "goalSource": goal_source,
        "autopilotPaused": getattr(record, "autopilot_paused", False),
        "autopilotCooldownUntil": cooldown_until,
        "autoArchiveOptOut": getattr(record, "auto_archive_opt_out_reason", None),
        "prs": [p.to_dict() for p in (prs or [])],
        "usageUnverified": getattr(record, "usage_unverified", False),
        "interruptedByRestart": getattr(record, "interrupted_by_restart", False),
        # Pre-restart status, and whether that status means dropped WORK (resumable,
        # #938's "Resume interrupted" batch) rather than a dropped ASK (which
        # InterruptedAskNotice / the orphaned-question re-offer handle instead).
        # This is the BROWSER serializer; SessionRecord.to_dict is the daemon→edge
        # RPC one. Both must emit these or the feature is dead on the wire.
        "interruptedFromStatus": getattr(record, "interrupted_from_status", None),
        # Why a finished session produced no PR: the mark_no_pr/marker token on a
        # 'complete' session, or the queue drainer's "obsolete: PR #N merged before
        # start" on a queued session it ended without ever spawning. Same
        # two-serializer rule as above — SessionRecord.to_dict already emits these;
        # without them here the card shows a bare "Ended" over an empty transcript.
        "prResolution": getattr(record, "pr_resolution", None),
        "prResolutionSource": getattr(record, "pr_resolution_source", None),
        # The hand-back ask (#2321) and the suspected-complete overlay on it (the
        # completion bar's reason). Same two-serializer rule: SessionRecord.to_dict
        # emits them daemon→edge, this emits them to the browser. #2326 added the
        # first to the RPC copy only, so the card never showed the ask it was built
        # to show (prod 2026-09-25: "Handed back" with nothing beside it).
        "handBackReason": getattr(record, "hand_back_reason", None),
        "suspectedCompleteReason": getattr(record, "suspected_complete_reason", None),
        "resumable": bool(getattr(record, "interrupted_by_restart", False)
                          and getattr(record, "interrupted_from_status", None)
                          in RESUME_ELIGIBLE_STATUSES),
        "summary": summary or "",
        "queuePosition": queue_position,
        "queueStallReason": queue_stall_reason,
        # "assistant" for the AI-drawer thread, null for a coding session. The drawer
        # fetches its thread by id (this list excludes assistants by default), so this
        # is what lets a client tell the two apart when it does get one.
        "kind": getattr(record, "kind", None),
    }
    # Liveness derived at the edge from the owning host's heartbeat snapshot (spec 002
    # FR-103). Never stored — always computed from host_heartbeats. Emitted ONLY when a
    # snapshot was available to this call (success or explicit failure): a mutation
    # endpoint that fetched no snapshot omits the keys, so a field-level cache merge
    # preserves whatever demotion the last list/get computed instead of overwriting it
    # with an unconditional "fresh" (item 7 of #961).
    if _liveness is not None:
        d["liveness"] = _liveness.state
        d["lastConfirmedAliveAt"] = (
            iso_utc(_liveness.last_confirmed_alive_at)
            if _liveness.last_confirmed_alive_at is not None else None)
    return d


async def _resolve_repo(record) -> str | None:
    """Read the repo indicator live from the session's working dir.

    Runs git in ``record.cwd`` (off the event loop) so the sessions screen shows
    what's actually checked out — a worktree resolves to its clone's origin, and
    a scratch dir with no git remote reports None ("Scratch"). The live read is
    preferred whenever it yields something, since it reflects the working dir's
    actual current state (e.g. after a branch switch).

    Falls back to the stored ``record.repo`` whenever the live read yields
    nothing — not only when ``cwd`` is empty, but also when ``cwd`` is set and
    the live read returns None/empty or raises. This matters because the
    session daemon can run on a different host from this (edge) process: ``cwd``
    then points to a real path on the daemon's filesystem that doesn't exist
    here, the live read silently returns None, and without this fallback every
    session would render as "Scratch" even though the DB has the correct repo.
    This is strictly safer on a single host too — there, an unreadable cwd
    means a deleted or broken worktree, and the stored repo is still better
    information than "Scratch". A genuine scratch session (a real directory
    with no git remote) still resolves to None here, because in that case
    ``record.repo`` is itself None — no sentinel is invented.
    """
    cwd = getattr(record, "cwd", None)
    if not cwd:
        return record.repo
    try:
        live = await asyncio.to_thread(gitrepos.repo_at_path, cwd)
    except Exception:
        return record.repo
    return live if live else record.repo


async def _session_is_archived(orch, session_id: str) -> bool:
    """Whether ``session_id`` currently appears in the archived list.

    Used to reconcile a lost/timed-out archive RPC: if the daemon completed the
    archive but its response was lost, the session shows up here. A daemon that is
    genuinely unreachable re-raises ``DaemonUnavailable`` (surfacing the real 503)
    rather than being mistaken for "not archived".
    """
    archived = await orch.list_archived()
    return any(a.get("id") == session_id for a in archived)


async def _session_still_interrupted(orch, session_id: str) -> bool:
    """Whether ``session_id`` is still flagged interrupted-by-restart — reconciles a
    timed-out resume RPC whose daemon-side work may have completed. A missing record
    or a failed re-read counts as still interrupted (never claim success unproven)."""
    try:
        rec = await orch.get_session(session_id)
    except Exception:  # noqa: BLE001 — reconciliation is best-effort
        return True
    return rec is None or bool(getattr(rec, "interrupted_by_restart", False))


# Per-session enrichment calls (suggestions, prs, summary) are daemon RPCs with no
# inherent timeout — one wedged session would otherwise hold up the whole response.
# Gate each RPC individually so a single bad session degrades to an empty field instead
# of stalling the list, even when the daemon is under memory pressure or mid-restart.
#
# The deadline is pushed INTO each RPC (call_with_deadline → DaemonClient.call → httpx),
# never raced from outside with asyncio.wait_for. wait_for CANCELS the request, and a
# CancelledError thrown into httpcore orphans the socket and strands its pool slot ACTIVE
# forever — 63 cancellable RPCs per page load wedged the edge twice in two days
# (issue #734, spike 2026-08-23-edge-socket-leak.md).
#
# WHAT THIS DOES *NOT* BOUND: httpx's read timeout is per-read — the gap between bytes —
# not a whole-response budget. A daemon that STALLS is cut off after this many seconds; a
# daemon that drips a response slowly can exceed it, so total /api/sessions latency is no
# longer strictly wall-clock bounded the way the old outer wait_for made it. That is
# inherent to the fix: any outer bound reintroduces the cancellation being removed. If a
# whole-response budget is ever needed it has to come from the daemon side (an RPC that
# answers or gives up), not from an asyncio timer on the edge.
ENRICH_FETCH_TIMEOUT = float(os.environ.get("VIBERPIT_ENRICH_FETCH_TIMEOUT", "3.0"))

# A daemon-connectivity blip (ConnectTimeout, PoolTimeout, ...) makes every page poll
# during the blip fail the same batched RPC, so a single multi-minute outage turns into
# thousands of identical WARNING+traceback records — the error sink files one auto-error
# issue per fingerprint (#1722, 1998 occurrences of one ConnectTimeout over a week) but
# still buries the signal under its own repeats in the journal. Windowed per RPC name so
# a genuinely new failure (a different batch breaking) still reports promptly.
_ENRICH_BATCH_FAIL_LOG = LogThrottle()


_QUEUE_POSITIONS_FAIL_LOG = LogThrottle()


async def _fetch_queue_positions(orch: Any, caller: str) -> dict[str, int]:
    """Best-effort queue positions; any failure degrades to empty.

    A refused/timed-out daemon connect is a transient blip, not a defect, so it logs at
    INFO (throttled) — WARNING is what the error sink files as an issue (#3158).
    """
    fn = getattr(orch, "session_queue_positions", None)
    if fn is None:
        return {}
    try:
        res = fn()
        if inspect.isawaitable(res):
            res = await res
        return res or {}
    except Exception as exc:
        level = logging.INFO if isinstance(exc, DaemonUnavailable) else logging.WARNING
        log_event_throttled(log, _QUEUE_POSITIONS_FAIL_LOG, caller, level,
                            "session_queue_positions.fetch_failed",
                            caller=caller, exc_info=True)
        return {}


async def _enrich_batch(orch, records, viewing: bool, timeout: float):
    """Fetch every session's enrichment in three batched RPCs instead of three per session.

    Returns (repos, suggestions, prs, summaries), each positionally aligned to *records*.

    A session id absent from a reply degrades to that field's empty value, and a whole
    batch that fails degrades only its own field — the other two still render (#742).

    Each RPC's deadline is pushed IN via call_with_deadline. Do NOT wrap these in
    asyncio.wait_for: it cancels the request, stranding the connection's pool slot and
    wedging the edge (#734).
    """
    ids = [r.id for r in records]
    if not ids:
        return (), (), (), ()

    async def _batch(name, **extra):
        """One batched RPC; any failure degrades to empty."""
        fn = getattr(orch, name, None)
        if fn is None:
            return {}
        try:
            return await call_with_deadline(fn, ids, timeout=timeout, **extra) or {}
        except Exception as exc:
            # A refused/timed-out connect is a transient daemon blip (restart, deploy
            # rollout), not a defect: the routes that need the daemon 503 on their own and
            # these fields just render empty. WARNING here is what made the error sink file
            # #1668 (155+ occurrences of one ConnectError), so keep it below the sink's
            # threshold outright rather than only after the throttle window.
            level = logging.INFO if isinstance(exc, DaemonUnavailable) else logging.WARNING
            log_event_throttled(log, _ENRICH_BATCH_FAIL_LOG, name, level,
                                 "list_sessions.enrich_batch_failed",
                                 rpc=name, session_count=len(ids), exc_info=True)
            return {}

    repos, sugg_map, pr_map, sum_map = await asyncio.gather(
        asyncio.gather(*(_resolve_repo(r) for r in records), return_exceptions=True),
        _batch("latest_suggestions_many"),
        _batch("prs_for_many"),
        _batch("latest_summary_many", viewing=viewing),
    )
    resolved_repos = [
        repo if not isinstance(repo, BaseException) else getattr(r, "repo", None)
        for r, repo in zip(records, repos)
    ]
    return (
        resolved_repos,
        [sugg_map.get(r.id) or [] for r in records],
        [pr_map.get(r.id) or [] for r in records],
        [sum_map.get(r.id) or "" for r in records],
    )


async def _autopilot_defer_seconds() -> int | None:
    """Human-deference window (seconds) for the cooldown badge, or None when autopilot is
    disabled/off (→ no badge). Reads the autopilot Automation row directly: the edge process owns
    DB access, and the orchestrator's `_autopilot_read_config` is not exposed on the edge adapter.
    Mirrors the row-read pattern in server/api/automations.py — including its schema: the
    validation goes through ``registry.schema_for`` (automation fields PLUS whatever its
    engine ceiling adds), never the bare ``config_schema``. Autopilot is ``claude_only``
    today so the two are identical, but reading through the narrow half is what silently
    reset the backlog runner's config; here the bare ``except`` below would have swallowed
    it into a missing badge."""
    from sqlalchemy import select
    from server.automations.base import retired_config_keys_of, validate_config
    from server.automations.registry import registry
    from server.db.models import Automation
    try:
        async with db_session() as db:
            row = (await db.execute(
                select(Automation).where(Automation.id == "autopilot"))).scalar_one_or_none()
        if row is None or not row.enabled or row.mode == "off":
            return None
        cfg = validate_config(registry.schema_for("autopilot"), dict(row.config or {}),
                              retired=retired_config_keys_of(registry.get("autopilot")))
        return int(cfg.get("human_defer_seconds", 120))
    except Exception:
        return None


async def _compute_queue_stall_reason(records: list) -> dict[str, Any] | None:
    """Why the queue can't drain right now, or ``None`` when nothing is wrong.

    The 2026-08-22 deadlock: 15 queued sessions, capacity fully occupied by
    finished sessions that were exempt from auto-archive, and the UI showed
    nothing but a list. Finding the cause took a DB query, a source read, and
    a py-spy dump of a production process — this makes that same fact visible
    on the endpoint that already renders the queue.

    Capacity-occupancy is read straight off ``CAPACITY_EXEMPT_STATUSES`` (Task
    2's source of truth) applied to the same ``records`` the list endpoint
    already fetched — not re-derived, not a second query. Only the numeric cap
    itself requires a DB read (``global_cap.read_global_cap``).

    Scope is deliberately narrow: this only names the capacity-full cause.
    An empty queue, or a queue with room to start, reports nothing — there is
    nothing to explain.
    """
    from server.db.models import CAPACITY_EXEMPT_STATUSES
    queued_count = sum(1 for r in records if r.status == "queued")
    if queued_count == 0:
        return None
    cap_occupying = sum(1 for r in records if r.status not in CAPACITY_EXEMPT_STATUSES)
    from server.sessions.global_cap import read_global_cap
    async with db_session() as db:
        cap = (await read_global_cap(db)).effective
    if cap_occupying < cap:
        return None
    return {
        "code": "capacity_full",
        "message": (
            f"{cap_occupying} of {cap} active-session slot(s) are occupied, so "
            f"{queued_count} queued session(s) cannot start. End or archive an "
            "occupying session to free a slot."
        ),
        "capOccupying": cap_occupying,
        "cap": cap,
        "queuedCount": queued_count,
    }


#: How long a queued session may sit with free fleet capacity before the list
#: endpoint owes the operator an explanation. A drain normally starts an entry
#: within seconds; under this, "queued" is just "about to start".
QUEUE_WAIT_GRACE_S = 60.0


_JUST_QUEUED_MESSAGES = {
    "global_cap": ("Every active-session slot is in use; this session starts when one "
                   "frees. Use Start now to override."),
    "host_capacity": ("Every host is at its session cap; this session starts when one "
                      "has room. Use Start now to override."),
    "product_capacity": ("This product is at its in-flight cap; this session starts when "
                         "a slot frees. Use Start now to override."),
    "runner_capacity": ("Backlog Runner capacity is full; this session starts when a slot "
                        "frees. Use Start now to override."),
    # Reached when the edge's own probe already sees headroom again (a race with
    # the create): still say why it queued, and never offer Start now for it.
    "account_limited": ("Every account that can run this model is at its usage ceiling; "
                        "this session starts automatically when quota frees."),
}


async def _queue_stall_reasons(
    records: list, *, grace_s: float = QUEUE_WAIT_GRACE_S,
) -> dict[str, dict[str, Any]]:
    """``{session id: why it is still queued}`` for every queued record that has
    waited — never None for one of those (incident 2026-10-06: ten sessions sat
    queued for 14 h with free hosts and accounts and ``queueStallReason: null``,
    because the only cause this endpoint could name was the GLOBAL cap).

    The global cause wins and is shared by every queued session, as before.
    Otherwise the reason is per session, read from the ONE place the dispatcher
    recorded it — the ``session_queue`` entry's ``queue_reason`` — and re-checked
    against the same gate the drain re-checks (``repo_capacity_status`` /
    ``queued_claim_slot_free``), so the message says what is binding NOW.
    Best-effort: any read failure degrades to no per-session reason, never a 500.
    """
    queued = [r for r in records if r.status == "queued"]
    if not queued:
        return {}
    shared = await _compute_queue_stall_reason(records)
    if shared is not None:
        return {r.id: shared for r in queued}
    try:
        return await _per_session_stall_reasons(queued, grace_s=grace_s)
    except Exception:
        log.warning("list_sessions: per-session queue stall reasons failed", exc_info=True)
        return {}


async def _per_session_stall_reasons(
    queued: list, *, grace_s: float = QUEUE_WAIT_GRACE_S,
) -> dict[str, dict[str, Any]]:
    from datetime import timezone
    from sqlalchemy import select
    from server.automations.context import AutomationContext
    from server.automations.product_admission import queued_claim_slot_free, repo_capacity_status
    from server.db.models import SessionQueue

    def _aware(value: datetime | None) -> datetime | None:
        return value if value is None or value.tzinfo else value.replace(tzinfo=timezone.utc)

    now = datetime.now(timezone.utc)
    async with db_session() as db:
        entries = {row.session_id: row for row in (await db.scalars(
            select(SessionQueue).where(SessionQueue.session_id.in_([r.id for r in queued])))).all()}
    context = AutomationContext("backlog-runner")
    product_status: dict[str, tuple[str | None, int | None, int | None]] = {}
    # One read-only account probe per (engine, quota model): N queued sessions, one probe.
    account_probe: dict[tuple[Any, str | None], AllAccountsBusy | None] = {}
    out: dict[str, dict[str, Any]] = {}
    for record in queued:
        entry = entries.get(record.id)
        since = _aware(entry.enqueued_at if entry is not None
                       else getattr(record, "created_at", None))
        if since is None or (now - since).total_seconds() < grace_s:
            continue
        waited = int((now - since).total_seconds())
        if entry is None:
            out[record.id] = {
                "code": "no_queue_entry", "waitedSeconds": waited,
                "message": "This session is marked queued but has no queue entry, so "
                           "nothing will start it. Archive it to clear it."}
            continue
        params = dict(entry.params or {})
        kind, repo = params.get("queue_reason", "global_cap"), params.get("repo")
        if kind == "product_capacity" and repo:
            if repo not in product_status:
                product_status[repo] = await repo_capacity_status(repo, context=context)
            slug, count, cap = product_status[repo]
            if slug is not None and count is not None and cap is not None and count >= cap:
                out[record.id] = {
                    "code": "product_capacity_full", "waitedSeconds": waited,
                    "product": slug, "inFlight": count, "cap": cap,
                    "message": (f"Product {slug} is at its in-flight cap ({count}/{cap}), so "
                                "this session waits for a slot there. Raise the product's "
                                "cap on the Capacity page or wait for a run to finish.")}
                continue
        elif kind == "runner_capacity" and repo:
            if not await queued_claim_slot_free(repo, session_id=record.id, context=context):
                out[record.id] = {
                    "code": "runner_capacity_full", "waitedSeconds": waited,
                    "message": "Backlog Runner capacity (the runner ceiling, the product's "
                               "in-flight or pipeline cap) is full, so this dispatch waits "
                               "for a slot."}
                continue
        elif kind == "account_limited" and not params.get("account"):
            limit: AllAccountsBusy | None = None
            try:
                engine = parse_agent_engine(params.get("engine"), legacy_default=True)
                model = quota_model(params.get("model"), engine=engine)
                if (engine, model) not in account_probe:
                    try:
                        # Read-only probe, as SessionOrchestrator._account_headroom_or_raise;
                        # no `exclude`: the edge holds no quarantine state.
                        await asyncio.to_thread(
                            select_account, reserve=False, engine=engine, model=model)
                        account_probe[(engine, model)] = None
                    except AllAccountsBusy as busy:
                        account_probe[(engine, model)] = busy
                limit = account_probe[(engine, model)]
            except Exception:
                # Degrade only this entry: its reason falls through, the rest stay.
                log.warning("queue stall reason: account probe failed for %s",
                            record.id, exc_info=True)
            if limit is not None:
                reset = iso_utc(limit.earliest_reset)
                if limit.kind == "no_usable_accounts":
                    why = ("No account that can run this model is usable (accounts need "
                           "re-authentication), so this session waits")
                else:
                    why = ("Every account that can run this model is at its usage "
                           "ceiling, so this session waits for quota")
                out[record.id] = {
                    "code": "account_limited", "waitedSeconds": waited, "earliestReset": reset,
                    "message": (why + (f" (earliest reset {reset})" if reset else "")
                                + ". It starts automatically; Start now cannot override "
                                  "an account limit.")}
                continue
        if grace_s <= 0:
            # Just queued: it was queued BECAUSE something is full, so never claim
            # "capacity is free". Say which gate held it.
            out[record.id] = {
                "code": "queued", "waitedSeconds": waited,
                "message": _JUST_QUEUED_MESSAGES.get(kind, _JUST_QUEUED_MESSAGES["global_cap"])}
            continue
        out[record.id] = {
            "code": "waiting_for_drain", "waitedSeconds": waited,
            "message": "Capacity is free, but no host has started this session yet: its "
                       "owning host may be full or unreachable, or the queue has not been "
                       "drained since the slot freed."}
    return out


def _get_orchestrator(request: Request) -> "InProcessOrchestratorAdapter":
    return request.app.state.sessions


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("")
async def list_sessions(
    request: Request,
    _identity: str = Depends(current_identity),
) -> list[dict]:
    """Return all in-memory session records.

    Assistant threads are a UI surface, not dashboard cards, so they are excluded by
    default. `?kind=assistant` SELECTS them — it returns assistant threads only, not
    everything-plus-assistants; the orchestrator flag merely stops them being filtered
    out upstream, and the narrowing happens here. The kwarg is passed ONLY when asked
    for, so the default call is byte-identical for every orchestrator flavour
    (in-process, remote, fleet) and for test doubles.
    """
    orch = _get_orchestrator(request)
    want_assistant = request.query_params.get("kind") == ASSISTANT_KIND
    records = await orch.list_sessions(**({"include_assistant": True} if want_assistant else {}))
    if want_assistant:
        records = [r for r in records if is_assistant(r)]
    viewing = request.query_params.get("viewing") in ("1", "true", "yes")
    # Enrich the whole page in three batched RPCs (one per field) rather than three per
    # session: 21 sessions cost 3 daemon round trips, not 63 (#742). Each is bounded by
    # ENRICH_FETCH_TIMEOUT, pushed INTO the RPC — never raced with wait_for (#734).
    repos, suggs, prs, summaries = await _enrich_batch(
        orch, records, viewing, ENRICH_FETCH_TIMEOUT)
    defer_seconds = await _autopilot_defer_seconds()
    positions = await _fetch_queue_positions(orch, "list_sessions")
    stall_reasons = await _queue_stall_reasons(records)
    heartbeats: Any = _SNAPSHOT_FAILED
    hb_now: datetime | None = None
    try:
        _snap = await host_heartbeat.snapshot()
        heartbeats, hb_now = _snap.hosts, _snap.now
    except Exception:                          # DB down: report "unknown", never false-fresh (NFR-5)
        log.warning("list_sessions: heartbeat snapshot failed — liveness degrades to unknown",
                    exc_info=True)
    return [
        _record_to_dict(
            r, repo=repo, suggestions=s, prs=pr_list, summary=summary,
            defer_seconds=defer_seconds, queue_position=positions.get(r.id),
            queue_stall_reason=stall_reasons.get(r.id),
            heartbeats=heartbeats, heartbeats_now=hb_now)
        for r, repo, s, pr_list, summary in zip(records, repos, suggs, prs, summaries)
    ]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_session(
    body: CreateSessionRequest,
    request: Request,
    identity: str = Depends(current_identity),
    client_node: str | None = Depends(current_client_node),
    db=Depends(get_db),
):
    """Create a native-agent session on the most-headroom compatible account.

    Returns 409 with earliestReset if all accounts are at ceiling.
    """
    orch = _get_orchestrator(request)
    if body.reasoningEffort is not None and body.engine is AgentEngine.CLAUDE_CODE:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "error": {
                    "code": "unsupported_reasoning_effort",
                    "message": (
                        "reasoningEffort is not supported by agent engine: "
                        f"{body.engine.value}"
                    ),
                    "engine": body.engine.value,
                },
            },
        )
    # When the user supplies no name, the session is auto-named (Claude title / first
    # prompt) and stays auto until a manual rename. A supplied name is pinned manual.
    name_is_auto = not (body.name and body.name.strip())
    name = body.name or "New Session"

    # Preset / permission-profile resolution (Task 7.4, #1228). A preset only
    # fills fields the caller left unset — it never overwrites an explicit
    # value. `tool_allowlist` is NOT threaded through orch.create_session: its
    # only reader is the standing-rule gate, which reads the DB row, so it's
    # applied as a direct DB write after the session record exists, below.
    from server.db.models import SessionPreset, PermissionProfile, Session as _DbSession

    _resolved_tool_allowlist: list[str] | None = None
    if body.presetId is not None:
        preset = await db.get(SessionPreset, body.presetId)
        if preset is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "not_found", "message": f"Preset {body.presetId!r} not found"},
            )
        if body.repo is None:
            body.repo = preset.repo
        if body.account is None:
            body.account = preset.account
        if body.model is None:
            body.model = preset.model
        if body.permissionMode is None:
            body.permissionMode = preset.permission_mode
        if body.initialPrompt is None:
            body.initialPrompt = preset.initial_prompt
        if body.permissionProfileId is None:
            body.permissionProfileId = preset.permission_profile_id

    if body.permissionProfileId is not None:
        profile = await db.get(PermissionProfile, body.permissionProfileId)
        if profile is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "not_found", "message": f"Permission profile {body.permissionProfileId!r} not found"},
            )
        if profile.patterns:
            _resolved_tool_allowlist = list(profile.patterns)

    # Validate repo format (nameWithOwner) before using it in URLs or filesystem paths.
    if body.repo is not None and not _REPO_RE.match(body.repo):
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "error": {
                    "code": "invalid_repo",
                    "message": (
                        f"repo must be 'owner/name' with only alphanumeric, '.', '_', '-' characters; "
                        f"got {body.repo!r}"
                    ),
                },
            },
        )

    # Validate account name — must be well-formed before any filesystem access,
    # then must exist in the local credential store.
    _ACCOUNT_RE = re.compile(r"^[A-Za-z0-9._-]+$")
    if body.account is not None:
        if not _ACCOUNT_RE.match(body.account):
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={
                    "error": {
                        "code": "unknown_account",
                        "message": f"account {body.account!r} not found",
                    },
                },
            )
        # Check account existence in a provider-aware way: anthropic accounts have a
        # slot file; copilot accounts have a token directory.
        known_accounts = {name for name, _ in _accounts.all_accounts()}
        if body.account not in known_accounts:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={
                    "error": {
                        "code": "unknown_account",
                        "message": f"account {body.account!r} not found",
                    },
                },
            )

    # repo (nameWithOwner) is resolved into a ViberPit-owned clone path — cloning
    # on first use — by the orchestrator itself (server/sessions/orchestrator.py
    # create_session), NOT here.  gitrepos.ensure_clone owns the on-disk clone
    # registry, and in a dual-host deployment the orchestrator may be running in
    # the session daemon on a different host than this edge process: cloning
    # here would put the checkout on the wrong host's filesystem (a live
    # production bug — see server/sessions/remote.py and server/api/repos.py for
    # why ensure_clone is daemon-owned). Passing repo= through and leaving cwd
    # unset lets create_session resolve it wherever it actually runs.

    # Permission mode: explicit → pinned-account default → global Settings default
    # → interactive. One resolver shared with adopt_session (see permissions.py) so
    # no create path can drift back to a hardcoded "interactive".
    _resolved_permission_mode = await resolve_permission_mode(
        db, explicit=body.permissionMode, account=body.account)
    # Model: the pinned account's own default is the only fallback (only when the
    # account is explicitly pinned); "account default" otherwise.
    _resolved_model = body.model
    if body.account and not _resolved_model:
        from viberpit import ledger as _ledger
        # Read-only, off-loop: no flock, no fsync'd write-back on the event loop (#3150).
        _led = await asyncio.to_thread(_ledger.read_ledger)
        _resolved_model = _ledger.get_account_defaults(_led, body.account).get("defaultModel")
    _resolved_effort = body.reasoningEffort

    # Resolve request -> account -> engine defaults as one pair, then dispatch
    # exactly the pair the selected account's live catalog validated. Validation
    # before account-default resolution accepts stale defaults without checking;
    # ignoring the verdict silently discards engine-default substitution.
    if body.engine is AgentEngine.CODEX and body.account:
        verdict = await orch.codex_validate_model(
            body.account, _resolved_model, _resolved_effort
        )
        if not verdict.get("ok", True):
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content={"error": verdict.get("error") or {
                    "code": "model_effort_mismatch"}},
            )
        _resolved_model = verdict.get("model", _resolved_model)
        _resolved_effort = verdict.get("effort", _resolved_effort)

    # Per-session toggles → the existing opt-out columns. Sent ONLY when a toggle
    # is off so the governed-by-default request stays byte-identical: a daemon that
    # predates the toggles binds create_session(**params) and would TypeError on an
    # unknown key (same rolling-upgrade posture as client_node/display).
    _toggle_kwargs: dict = {}
    if body.autopilot is False:
        _toggle_kwargs["autopilot_paused"] = True
    if body.autoArchive is False:
        _toggle_kwargs["auto_archive_opt_out_reason"] = AUTO_ARCHIVE_OPT_OUT_CREATE

    try:
        record = await orch.create_session(
            prefer=getattr(body, "host", None),
            name=name,
            account=body.account,
            initial_prompt=body.initialPrompt,
            cwd=None,
            branch=body.branch,
            repo=body.repo,
            identity=identity,
            permission_mode=_resolved_permission_mode or "interactive",
            attachment_ids=body.attachmentIds,
            name_is_auto=name_is_auto,
            model=_resolved_model,
            engine=body.engine,
            client_node=client_node,
            display=body.display,
            **({"reasoning_effort": _resolved_effort}
               if _resolved_effort is not None else {}),
            **_toggle_kwargs,
        )
    except FleetDispatchRefused as exc:
        # UnknownHost -> 400, NoEngineHostAvailable/NoHostAvailable -> typed 409.
        # Shared with the assistant drawer's create so the subclass ordering that
        # keeps "install the Codex binary" from reading as "no host available"
        # cannot drift between the two (#2184).
        return fleet_refusal_response(exc)
    except AgentEngineUnavailable as exc:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "error": {
                    "code": "engine_unavailable",
                    "message": str(exc),
                    "engine": exc.engine.value,
                },
            },
        )
    except (gitrepos.CloneError, gitrepos.GhError) as exc:
        # repo→clone resolution now happens inside orch.create_session (on
        # whichever process actually owns the clone registry — the daemon in
        # dual-host mode). The error contract is unchanged: still a 502 with
        # code "clone_failed". In-process this is the real exception raised
        # by gitrepos.ensure_clone; over the daemon RPC boundary it is the
        # same type, reconstructed by RemoteOrchestrator.create_session from
        # the DaemonError envelope (see server/sessions/remote.py).
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={
                "error": {
                    "code": "clone_failed",
                    "message": str(exc),
                },
            },
        )
    except ValueError as exc:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "error": {
                    "code": "invalid_cwd",
                    "message": str(exc),
                },
            },
        )
    except BranchSwitchError as exc:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "error": {
                    "code": "branch_switch_failed",
                    "message": str(exc),
                },
            },
        )
    except ModelBlockedError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="model blocked by policy")
    except AllAccountsBusy as exc:
        return _all_accounts_busy_response(
            exc, limited_message="All accounts are at or above the usage ceiling")

    if _resolved_tool_allowlist is not None:
        # Enforced by the standing-rule gate (server/permission_rules/gate.py):
        # each pattern that passes the rule safety check auto-allows matching
        # requests like a session-scope always-allow rule (#2952). It widens what
        # runs without a card; it never restricts which tools the session can use.
        from sqlalchemy import update as sa_update

        await db.execute(
            sa_update(_DbSession).where(_DbSession.id == record.id).values(
                tool_allowlist=_resolved_tool_allowlist)
        )
        await db.commit()

    # Audit: session.create
    await record_audit(
        db=db,
        type="session.create",
        identity=identity,
        detail={"session_id": record.id, "account": record.account, "name": name},
    )

    # #2229: a capacity-saturated request comes back with status="queued", not
    # a misleading "it started" shape — the position tells the caller how far
    # back it landed, same as list_sessions/get_session already surface.
    queue_position: int | None = None
    if record.status == "queued":
        queue_position = (await _fetch_queue_positions(orch, "create_session")).get(record.id)

    # Best-effort and grace-free: the caller learns WHY it queued in the same response.
    queue_stall_reason: dict[str, Any] | None = None
    if record.status == "queued":
        try:
            # Against the FULL session list, as GET /api/sessions does, so the shared
            # global-cap cause sees the real occupancy.
            queue_stall_reason = (
                await _queue_stall_reasons(await orch.list_sessions(), grace_s=0)
            ).get(record.id)
        except Exception:
            log.warning("create_session: queue stall reason failed", exc_info=True)

    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content=_record_to_dict(record, repo=await _resolve_repo(record),
                                queue_position=queue_position,
                                queue_stall_reason=queue_stall_reason),
    )


# NOTE: like /archived below, these static routes MUST precede the dynamic
# "/{session_id}" route or FastAPI captures "adoptable"/"adopt" as a session_id.
@router.get("/adoptable")
async def list_adoptable(
    request: Request,
    _identity: str = Depends(current_identity),
) -> dict:
    """Discover live claude sessions running outside ViberPit.

    Returns ALL session candidates (mapped, unmapped, already-tracked), each
    flagged with a skipReason where applicable — the dashboard renders adoptable
    ones with a force-takeover action and groups the rest as "not adoptable".
    """
    _require_adoption_enabled()
    orch = _get_orchestrator(request)
    # Discovery runs on the daemon host (its /proc, its account map), not this edge
    # process — see orchestrator.list_adoptable_candidates (#709 finding 4).
    return {"candidates": await orch.list_adoptable_candidates()}


@router.post("/adopt", status_code=status.HTTP_201_CREATED)
async def adopt_session_route(
    body: AdoptRequest,
    request: Request,
    identity: str = Depends(current_identity),
) -> dict:
    """Force-take-over an untracked live session: terminate it, resume in place.

    409 when the session is no longer adoptable (process gone, unmapped, or
    already tracked) — an expected outcome given processes churn fast, so the
    dashboard should re-scan rather than treat it as a hard error.
    """
    _require_adoption_enabled()
    orch = _get_orchestrator(request)
    try:
        rec, outcome = await orch.adopt_session(body.claudeSessionId, identity=identity)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="session no longer adoptable",
        )
    return {"session": _record_to_dict(rec), **outcome}


# NOTE: this static route MUST be declared before the dynamic "/{session_id}" route
# below, or FastAPI matches "/archived" against "/{session_id}" (session_id="archived")
# and returns 404 — which silently broke the entire archived-sessions feature.
@router.get("/archived")
async def list_archived_sessions(
    request: Request,
    _identity: str = Depends(current_identity),
) -> list[dict]:
    """Return all archived session metadata."""
    orch = _get_orchestrator(request)
    rows = await orch.list_archived()
    # The orchestrator's internal RPC wire temporarily carries the old Claude
    # alias for daemon-first rolling deploys. It is not part of the browser API.
    return [
        {key: value for key, value in row.items() if key != "claudeSessionId"}
        for row in rows
    ]


async def _search_ids(items, fetch_text, terms) -> list[str]:
    """items: list[(id, name)]; fetch_text: async (id)->str (transcript text, "" if
    none). Returns matching ids. Blank `terms` skips the fetches entirely.

    `fetch_text` resolves the transcript path AND reads it inside the orchestrator
    (transcript_search_text — daemon RPC in remote mode, in-process call otherwise),
    never in this edge process. Path strings resolved by the daemon are only valid
    on the daemon's own host, so reading them here would silently return "" once the
    daemon and edge are split across hosts — this is exactly that split.
    """
    async def _matches(sid: str, name: str) -> bool:
        text = ""
        if terms:
            # One session whose transcript text can't be fetched (dangling account
            # name, daemon hiccup) degrades to name-only matching; it must not
            # fail the whole search — the UI silently drops a failed filter.
            try:
                text = await fetch_text(sid)
            except Exception:
                log.warning("session.search transcript fetch failed id=%s", sid, exc_info=True)
                text = ""
        return session_matches(terms, name, text)

    flags = await asyncio.gather(*(_matches(i, n) for i, n in items))
    return [i for (i, _), ok in zip(items, flags) if ok]


@router.get("/search")
async def search_sessions(
    request: Request,
    q: str = "",
    scope: Literal["live", "archived"] = "live",
    _identity: str = Depends(current_identity),
) -> dict:
    """Return session IDs whose name or transcript matches every term in `q`.

    `scope=live` (default) searches active sessions; `scope=archived` searches
    archived rows. Registered before GET /{session_id} so FastAPI does not match
    `search` as an id.
    """
    orch = _get_orchestrator(request)
    terms = parse_terms(q)
    if scope == "archived":
        rows = await orch.list_archived()
        items = [(r["id"], r["name"]) for r in rows]
        ids = await _search_ids(items, orch.transcript_search_text, terms)
    else:
        records = await orch.list_sessions()
        items = [(r.id, r.name) for r in records]
        ids = await _search_ids(items, orch.transcript_search_text, terms)
    return {"ids": ids}


# ---------------------------------------------------------------------------
# Batch operations on archived sessions
#
# These static routes MUST be declared before the dynamic "/{session_id}"
# routes below, or "batch" is matched as a session_id.
# ---------------------------------------------------------------------------


class BatchSessionIdsRequest(BaseModel):
    ids: list[str] = Field(..., min_length=1, max_length=200)


@router.post("/batch/unarchive")
async def batch_unarchive_sessions(
    body: BatchSessionIdsRequest,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Unarchive multiple sessions. Best-effort: returns per-id succeeded/failed."""
    orch = _get_orchestrator(request)
    succeeded: list[str] = []
    failed: list[dict] = []
    for session_id in dict.fromkeys(body.ids):  # dedupe, preserve order
        try:
            record = await orch.unarchive(session_id)
        except Exception as exc:  # noqa: BLE001 — best-effort batch must not abort
            log.warning("batch unarchive failed for %s: %s", session_id, exc)
            failed.append({"id": session_id, "error": str(exc)})
            continue
        if record is None:
            failed.append({"id": session_id, "error": "not found"})
            continue
        succeeded.append(session_id)
        await record_audit(
            db=db,
            type="session.unarchive",
            identity=identity,
            detail={"session_id": session_id},
        )
    return {"succeeded": succeeded, "failed": failed}


@router.post("/batch/permanent-delete")
async def batch_permanent_delete_sessions(
    body: BatchSessionIdsRequest,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Permanently delete multiple sessions. Best-effort: returns per-id succeeded/failed."""
    orch = _get_orchestrator(request)
    succeeded: list[str] = []
    failed: list[dict] = []
    for session_id in dict.fromkeys(body.ids):  # dedupe, preserve order
        try:
            deleted = await orch.permanent_delete(session_id)
        except Exception as exc:  # noqa: BLE001 — best-effort batch must not abort
            log.warning("batch permanent-delete failed for %s: %s", session_id, exc)
            failed.append({"id": session_id, "error": str(exc)})
            continue
        if not deleted:
            failed.append({"id": session_id, "error": "not found"})
            continue
        succeeded.append(session_id)
        await record_audit(
            db=db,
            type="session.permanent_delete",
            identity=identity,
            detail={"session_id": session_id},
        )
    return {"succeeded": succeeded, "failed": failed}


@router.post("/batch/archive")
async def batch_archive_sessions(
    body: BatchSessionIdsRequest,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Archive multiple live sessions. Best-effort: returns per-id succeeded/failed."""
    orch = _get_orchestrator(request)
    succeeded: list[str] = []
    failed: list[dict] = []
    for session_id in dict.fromkeys(body.ids):  # dedupe, preserve order
        try:
            await orch.archive_session(session_id)
        except DaemonUnavailable:
            # The daemon may have completed the archive but lost its response.
            if not await _session_is_archived(orch, session_id):
                log.warning("batch archive failed for %s: DaemonUnavailable", session_id)
                failed.append({"id": session_id, "error": "daemon unavailable"})
                continue
        except Exception as exc:  # noqa: BLE001 — best-effort batch must not abort
            log.warning("batch archive failed for %s: %s", session_id, exc)
            failed.append({"id": session_id, "error": str(exc)})
            continue
        succeeded.append(session_id)
        await record_audit(
            db=db,
            type="session.archive",
            identity=identity,
            detail={"session_id": session_id},
        )
    return {"succeeded": succeeded, "failed": failed}


@router.post("/batch/resume")
async def batch_resume_sessions(
    body: BatchSessionIdsRequest,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Resume restart-interrupted sessions (re-attach + nudge). Best-effort:
    a resumed session lands in `succeeded`; a skip / no_driver lands in `failed`."""
    orch = _get_orchestrator(request)
    succeeded: list[str] = []
    failed: list[dict] = []
    for session_id in dict.fromkeys(body.ids):  # dedupe, preserve order
        try:
            result = await orch.resume_interrupted(session_id, identity)
        except DaemonTimeout as exc:
            # The daemon keeps rebuilding the driver after the edge gives up, so a
            # slow resume usually still lands. Re-read the record: no longer
            # interrupted ⇒ resumed. Expected under load — INFO, not an auto-error.
            if exc.outcome_unknown and not await _session_still_interrupted(orch, session_id):
                log.info("batch resume of %s timed out but the session resumed", session_id)
                result = "resumed"
            else:
                log.info("batch resume of %s timed out; still pending", session_id)
                failed.append({"id": session_id, "error": "resume timed out; still pending"})
                continue
        except Exception as exc:  # noqa: BLE001 — best-effort batch must not abort
            log.warning("batch resume failed for %s: %s", session_id, exc)
            failed.append({"id": session_id, "error": str(exc)})
            continue
        if result == "resumed":
            succeeded.append(session_id)
            await record_audit(
                db=db, type="session.resume", identity=identity,
                detail={"session_id": session_id},
            )
        else:
            failed.append({"id": session_id, "error": result})
    return {"succeeded": succeeded, "failed": failed}


@router.post("/batch/rotate")
async def batch_rotate_sessions(
    body: BatchSessionIdsRequest,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Move each session to the next best account and continue its conversation —
    the "Rotate stalled (N)" button. Best-effort: "rotated" lands in `succeeded`;
    any skip / no_driver lands in `failed` with the reason as `error`."""
    orch = _get_orchestrator(request)
    succeeded: list[str] = []
    failed: list[dict] = []
    for session_id in dict.fromkeys(body.ids):  # dedupe, preserve order
        try:
            result = await orch.rotate_to_best(session_id, identity)
        except Exception as exc:  # noqa: BLE001 — best-effort batch must not abort
            log.warning("batch rotate failed for %s: %s", session_id, exc)
            failed.append({"id": session_id, "error": str(exc)})
            continue
        if result in ("rotated", "rotated_new_thread"):
            succeeded.append(session_id)
            await record_audit(db=db, type="session.rotate", identity=identity,
                               detail={"session_id": session_id, "batch": True})
        else:
            failed.append({"id": session_id, "error": result})
    return {"succeeded": succeeded, "failed": failed}


async def _stored_session_dict(session_id: str) -> dict | None:
    """The wire dict for a session no live record holds, read from its DB row.

    Uses the orchestrator's one row→record mapping, so this cannot drift from what a
    rehydrate or unarchive would build. No liveness keys: there is no driver to be
    live. ``archivedAt``/``archivedBy`` say why it left the live list.
    """
    from server.db import db_session as _db_session
    from server.db.models import Session as _DbSession
    from server.sessions.orchestrator import SessionOrchestrator
    from sqlalchemy import select as sa_select

    async with _db_session() as db:
        row = (await db.execute(
            sa_select(_DbSession).where(_DbSession.id == session_id)
        )).scalars().first()
    if row is None:
        return None
    d = _record_to_dict(SessionOrchestrator._record_from_row(row))
    d["archivedAt"] = iso_utc(row.archived_at)
    d["archivedBy"] = row.archived_by
    return d


@router.get("/{session_id}")
async def get_session(
    session_id: str,
    request: Request,
    _identity: str = Depends(current_identity),
) -> dict:
    """Return a single session record, or 404 if no row exists at all.

    The orchestrator only holds live records, so an archived/ended session is served
    from its DB row, with its real status. Before #3082 it 404'd while its transcript
    and export (which already fall back to the row) still served, and the assistant
    read that 404 as "the session vanished".
    """
    orch = _get_orchestrator(request)
    record = await orch.get_session(session_id)
    if record is None:
        stored = await _stored_session_dict(session_id)
        if stored is not None:
            return stored
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": f"Session {session_id!r} not found"},
        )
    queue_position: int | None = None
    queue_position = (await _fetch_queue_positions(orch, "get_session")).get(session_id)
    heartbeats: Any = _SNAPSHOT_FAILED
    hb_now: datetime | None = None
    try:
        _snap = await host_heartbeat.snapshot()
        heartbeats, hb_now = _snap.hosts, _snap.now
    except Exception:
        log.warning("get_session: heartbeat snapshot failed — liveness degrades to unknown",
                    exc_info=True)
    return _record_to_dict(record, repo=await _resolve_repo(record),
                           queue_position=queue_position,
                           heartbeats=heartbeats, heartbeats_now=hb_now)


@router.patch("/{session_id}")
async def rename_session(
    session_id: str,
    body: RenameSessionRequest,
    request: Request,
    _identity: str = Depends(current_identity),
) -> dict:
    """Rename a session. Returns 404 if unknown."""
    orch = _get_orchestrator(request)
    record = await orch.get_session(session_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": f"Session {session_id!r} not found"},
        )
    await orch.rename_session(session_id, body.name)
    # Re-fetch (rename_session updates in place)
    updated = await orch.get_session(session_id)
    final = updated or record
    return _record_to_dict(final, repo=await _resolve_repo(final))


@router.post("/{session_id}/autopilot")
async def set_autopilot_paused(
    session_id: str,
    body: AutopilotPauseRequest,
    request: Request,
    _identity: str = Depends(current_identity),
) -> dict:
    """Pin a session to manual (paused=true) or return it to autopilot governance.
    Returns the updated session record, or 404 if unknown."""
    orch = _get_orchestrator(request)
    record = await orch.get_session(session_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": f"Session {session_id!r} not found"},
        )
    await orch.set_autopilot_paused(session_id, body.paused)
    updated = await orch.get_session(session_id)
    final = updated or record
    return _record_to_dict(final, repo=await _resolve_repo(final))


@router.post("/{session_id}/auto-archive")
async def set_auto_archive_opt_out(
    session_id: str,
    body: AutoArchiveOptOutRequest,
    request: Request,
    _identity: str = Depends(current_identity),
) -> dict:
    """Opt a session out of auto-archive (optedOut=true) or restore its eligibility.
    Returns the updated session record, or 404 if unknown."""
    orch = _get_orchestrator(request)
    record = await orch.get_session(session_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": f"Session {session_id!r} not found"},
        )
    await orch.set_auto_archive_opt_out(session_id, body.optedOut)
    updated = await orch.get_session(session_id)
    final = updated or record
    return _record_to_dict(final, repo=await _resolve_repo(final))


@router.post("/{session_id}/suspected-complete")
async def resolve_suspected_complete(
    session_id: str,
    body: SuspectedCompleteRequest,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Resolve the suspected-complete overlay on a handed-back session.

    ``confirm`` archives it through the ordinary archive path with a distinct
    provenance context, ``reject`` clears the overlay so the card reads as a plain
    hand-back again. 404 if unknown, 409 if the session carries no such verdict —
    the two buttons only render on one, so a 409 means the user is acting on a
    stale card. Audited as ``session.suspected_complete.resolve`` so the
    confirm/reject ratio can be read before anyone proposes acting on the verdict
    automatically."""
    orch = _get_orchestrator(request)
    record = await orch.get_session(session_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": f"Session {session_id!r} not found"},
        )
    verdict = getattr(record, "suspected_complete_reason", None)
    if not verdict:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "not_suspected",
                    "message": f"Session {session_id!r} is not flagged as likely complete"},
        )
    if body.decision == "confirm":
        await orch.archive_session(
            session_id, archived_by="user",
            context={"reason": "suspected complete — confirmed by user", "bar": verdict})
    else:
        await orch.dismiss_suspected_complete(session_id)
    await record_audit(
        db=db,
        type="session.suspected_complete.resolve",
        identity=identity,
        detail={"session_id": session_id, "decision": body.decision, "reason": verdict},
    )
    return {"decision": body.decision, "sessionId": session_id}


@router.post("/{session_id}/queue-move")
async def queue_move_session(
    session_id: str,
    body: QueueMoveRequest,
    request: Request,
    _identity: str = Depends(current_identity),
) -> dict:
    """Reorder a queued session. Returns 404 if the session is not queued or unknown."""
    orch = _get_orchestrator(request)
    record = await orch.get_session(session_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": f"Session {session_id!r} not found"},
        )
    ok = await orch.session_queue_move(session_id, body.op, body.position)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_queued", "message": f"Session {session_id!r} is not in the queue"},
        )
    return {"ok": True}


@router.delete("/{session_id}/queue", status_code=status.HTTP_204_NO_CONTENT)
async def queue_cancel_session(
    session_id: str,
    request: Request,
    _identity: str = Depends(current_identity),
):
    """Remove a queued session from the queue and mark it ended. Returns 404 if not queued."""
    orch = _get_orchestrator(request)
    record = await orch.get_session(session_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": f"Session {session_id!r} not found"},
        )
    ok = await orch.session_queue_cancel(session_id)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_queued", "message": f"Session {session_id!r} is not in the queue"},
        )


def _all_accounts_busy_response(exc: AllAccountsBusy, *, limited_message: str) -> JSONResponse:
    """The 409 for an ``AllAccountsBusy``: ``accounts_need_reauth`` when no account is
    usable at all, else ``all_accounts_limited`` with the ``earliestReset``."""
    if exc.kind == "no_usable_accounts":
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "error": {
                    "code": "accounts_need_reauth",
                    "message": "No usable accounts — one or more need re-authentication",
                },
            },
        )
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={
            "error": {"code": "all_accounts_limited", "message": limited_message},
            "earliestReset": iso_utc(exc.earliest_reset),
        },
    )


async def _queue_reason_of(session_id: str) -> str | None:
    """The ``queue_reason`` the dispatcher recorded on a session's queue entry, for
    the audit trail. Best-effort: None when there is no entry or it cannot be read."""
    from server.sessions import session_queue as sq
    try:
        entry = await sq.entry_for(session_id)
    except Exception:
        log.warning("queue_start_now: queue entry unreadable id=%s", session_id, exc_info=True)
        return None
    if entry is None:
        return None
    return str((entry.params or {}).get("queue_reason", "global_cap"))


def _start_now_unavailable(code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        content={"error": {"code": code, "message": message}})


@router.post("/{session_id}/start-now")
async def queue_start_now(
    session_id: str,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
):
    """Start a queued session now, over slot caps and product holds (never over an
    account usage ceiling — that 409s with earliestReset and the session stays queued)."""
    orch = _get_orchestrator(request)
    record = await orch.get_session(session_id)
    if record is None or record.status != "queued":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_queued", "message": f"Session {session_id!r} is not in the queue"},
        )
    # Read before the start: a started session's queue entry is gone.
    queue_reason = await _queue_reason_of(session_id)
    try:
        outcome = await orch.start_queued_now(session_id)
    except AllAccountsBusy as exc:
        return _all_accounts_busy_response(
            exc, limited_message="Every account is at its usage ceiling; this session "
                                 "starts automatically when quota frees.")
    except DaemonUnavailable:
        log.warning("queue_start_now: daemon unavailable id=%s", session_id, exc_info=True)
        return _start_now_unavailable(
            "daemon_unavailable", "The session daemon could not be reached; try again shortly.")
    except DaemonError as exc:
        # A daemon that predates start_queued_now answers UnknownMethod: say so rather
        # than a bare 500 (#2857).
        log.warning("queue_start_now: daemon error id=%s type=%s", session_id, exc.err_type,
                    exc_info=True)
        if exc.err_type == "UnknownMethod":
            return _start_now_unavailable(
                "daemon_outdated", "The session daemon has not been updated to support "
                                   "Start now yet; try again after the next rollout.")
        return _start_now_unavailable(
            "start_failed_remote", f"The daemon could not start this session ({exc.err_type}).")
    if outcome == "not_queued":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_queued", "message": f"Session {session_id!r} is not in the queue"},
        )
    # Committed by get_db on a normal return, as for session.create.
    await record_audit(
        db=db,
        type="session.queue_start_now",
        identity=identity,
        detail={"session_id": session_id, "queue_reason": queue_reason, "outcome": outcome},
    )
    if outcome == "failed":
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "error": {
                    "code": "start_failed",
                    "message": "The session could not start and was ended; see the daemon log.",
                },
            },
        )
    return {"ok": True, "status": outcome}


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def close_session(
    session_id: str,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
):
    """Archive a session (status flip; worktree stays in place, DB row kept).

    Returns 404 if unknown. Disk is reclaimed later, when the
    max_archived_sessions cap purges the row.
    """
    from server.db.models import INACTIVE_SESSION_STATUSES, Session as _DbSession

    orch = _get_orchestrator(request)
    record = await orch.get_session(session_id)
    if record is None:
        # Orphaned-session fallback (#898): the owning daemon has lost the in-memory
        # record (process exited, daemon restarted, worktree removed) but the DB row
        # may still exist. A fleet primary loads ALL sessions into _records, but a
        # worker's get_session only sees its own. Verify via DB before returning 404
        # so orphaned sessions don't become permanently undeletable through the UI.
        _db_row = await db.get(_DbSession, session_id)
        if _db_row is None or _db_row.status in INACTIVE_SESSION_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "not_found", "message": f"Session {session_id!r} not found"},
            )
        # Session is known to DB but not to the owning daemon (orphaned). Proceed:
        # archive_session handles a missing _records entry gracefully (DB-only update).
    try:
        await orch.archive_session(session_id)
    except DaemonUnavailable:
        # The daemon may have completed the archive but lost/timed out its response
        # (it was briefly wedged or restarting). Reconcile against real state rather
        # than surface a spurious 503 for an op that actually took effect: if the
        # session now appears in the archived list, the archive succeeded.
        if not await _session_is_archived(orch, session_id):
            raise

    # Audit: session.archive
    await record_audit(
        db=db,
        type="session.archive",
        identity=identity,
        detail={"session_id": session_id},
    )


@router.post("/{session_id}/unarchive")
async def unarchive_session(
    session_id: str,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Restore an archived session to idle. Returns 404 if not found or not archived."""
    orch = _get_orchestrator(request)
    try:
        record = await orch.unarchive(session_id)
    except InsufficientStorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_507_INSUFFICIENT_STORAGE,
            detail={"code": "insufficient_storage", "message": str(exc)},
        )
    except ArchiveMissingError as exc:
        # 410: the session row is valid, but the archived bytes are not where the row
        # says they are. Surfacing this beats silently restoring an empty worktree.
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail={"code": "archive_missing", "message": str(exc)},
        )
    except ArchiveOwnedElsewhereError as exc:
        # 409: the row is fine, but this host is the wrong one to restore it (#3097).
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "archive_owned_elsewhere", "message": str(exc)},
        )
    except DaemonError as exc:
        if exc.err_type == "InsufficientStorageError":
            raise HTTPException(
                status_code=status.HTTP_507_INSUFFICIENT_STORAGE,
                detail={"code": "insufficient_storage", "message": exc.message},
            )
        if exc.err_type == "ArchiveMissingError":
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail={"code": "archive_missing", "message": exc.message},
            )
        if exc.err_type == "ArchiveOwnedElsewhereError":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "archive_owned_elsewhere", "message": exc.message},
            )
        raise
    except DaemonUnavailable:
        # As with archive: the daemon may have restored the session but lost/timed out
        # its response. Reconcile — if the session is live again, the unarchive took
        # effect; return it instead of a spurious 503.
        record = await orch.get_session(session_id)
        if record is None:
            raise
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": f"Archived session {session_id!r} not found"},
        )

    # Audit: session.unarchive
    await record_audit(
        db=db,
        type="session.unarchive",
        identity=identity,
        detail={"session_id": session_id},
    )
    return _record_to_dict(record, repo=await _resolve_repo(record))


def _resume_failed_detail(record: object) -> dict:
    """409 detail for an ensure_driver(...) None, tailored to the one reason
    ensure_driver's caller can already see on `record`: a queued session (#1777).
    Every other None cause (lease held by another host, a mid-flight spawn
    failure) is invisible to the API layer, so it keeps the generic message."""
    if getattr(record, "status", None) == "queued":
        return {
            "code": "resume_failed_queued",
            "message": "Session is still queued and hasn't started yet — it will "
            "resume automatically once capacity frees up.",
        }
    return {"code": "resume_failed", "message": "Session could not be resumed."}


async def resume_and_send_prompt(orch: Any, session_id: str, text: str, identity: str, *,
                                 interrupt: bool = False) -> tuple[Any, str]:
    """The one HTTP-side "deliver a prompt to a session" path: look it up (404),
    resume it if detached (409 when it can't be brought live), send. Returns the
    record and ``send_prompt``'s delivery result. Shared by the quick-resume route
    and any API that hands a session text on the operator's behalf (#2936)."""
    record = await orch.get_session(session_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": f"Session {session_id!r} not found"},
        )
    # Resume the session if it has no live driver (detached). ensure_driver is
    # idempotent for already-live sessions and returns None only if the session
    # can't be brought live.
    driver = await orch.ensure_driver(session_id, identity)
    if driver is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_resume_failed_detail(record),
        )
    return record, await orch.send_prompt(session_id, text, interrupt=interrupt)


@router.post("/{session_id}/prompt")
async def send_session_prompt(
    session_id: str,
    body: SendPromptRequest,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Send a prompt to a session from the dashboard (quick-resume).

    Lazily RESUMES a detached (idle/"Ready") session first — the same ensure_driver
    path the WebSocket prompt flow uses — so one tap from the list gets a stalled
    session moving. 404 if unknown; 409 if the session exists but can't be resumed."""
    orch = _get_orchestrator(request)
    record, delivery = await resume_and_send_prompt(orch, session_id, body.text, identity,
                                                    interrupt=body.interrupt)
    if delivery not in ("sent", "draining"):
        log.warning("quick-resume prompt not delivered session=%s result=%s",
                    session_id, delivery)
    if delivery == "sent":
        try:
            await observe_reply(
                identity, body.text, source=body.replySource,
                via=getattr(request.state, "via", None), has_attachments=False,
            )
        except Exception:
            # Learning is optional; never turn an accepted prompt into a failed send.
            log.info("quick_reply observation failed")
    await record_audit(db, "session.prompt", identity, {"sessionId": session_id})
    updated = await orch.get_session(session_id) or record
    return _record_to_dict(updated, repo=await _resolve_repo(updated))


class PermissionResponseRequest(BaseModel):
    decision: str


class QuestionAnswerRequest(BaseModel):
    selected: list[str] = Field(default_factory=list)
    mode: str = "option"


class PermissionModeRequest(BaseModel):
    mode: str


class SetModelRequest(BaseModel):
    model: str | None = None


def _action_error_response(exc: ActionError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.http,
        content={"detail": {"code": exc.code, "message": exc.message, **(exc.extra or {})}},
    )


@router.post("/{session_id}/interrupt")
async def interrupt_session(
    session_id: str,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Stop a session's in-flight turn (HTTP twin of the WS `interrupt` frame)."""
    orch = _get_orchestrator(request)
    via = getattr(request.state, "via", None)
    return await session_actions.interrupt(orch, db, session_id=session_id, identity=identity, via=via)


@router.post("/{session_id}/agents/{tool_use_id}/stop")
async def stop_session_subagent(
    session_id: str,
    tool_use_id: str,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
):
    """Stop one running Task sub-agent (the SDK ``stop_task`` control request).
    404 ``SUBAGENT_NOT_RUNNING`` when the sub-agent already finished or the session
    is not live."""
    orch = _get_orchestrator(request)
    via = getattr(request.state, "via", None)
    try:
        return await session_actions.stop_subagent(
            orch, db, session_id=session_id, tool_use_id=tool_use_id,
            identity=identity, via=via)
    except ActionError as exc:
        return _action_error_response(exc)


@router.get("/{session_id}/mcp")
async def session_mcp_status(
    session_id: str,
    request: Request,
    _identity: str = Depends(current_identity),
) -> dict:
    """Live MCP server status for a running session (spec 2026-10-09)."""
    servers = await _get_orchestrator(request).mcp_status_for(session_id)
    if servers is None:
        raise HTTPException(status_code=404, detail={"code": "SESSION_NOT_LIVE"})
    return {"servers": servers}


@router.post("/{session_id}/mcp/{server}/reconnect")
async def session_mcp_reconnect(
    session_id: str,
    server: str,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
):
    """Reconnect one MCP server in a live session. 404 not live, 409 reconnect failed."""
    if not valid_mcp_name(server.rsplit(":", 1)[-1]):
        raise HTTPException(status_code=400, detail={"code": "INVALID_SERVER"})
    try:
        return await session_actions.mcp_reconnect(
            _get_orchestrator(request), db, session_id=session_id, server=server,
            identity=identity, via=getattr(request.state, "via", None))
    except ActionError as exc:
        return _action_error_response(exc)


class McpToggleRequest(BaseModel):
    enabled: bool


@router.post("/{session_id}/mcp/{server}/toggle")
async def session_mcp_toggle(
    session_id: str,
    server: str,
    body: McpToggleRequest,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
):
    """Enable/disable one MCP server in a live session. 404 kill switch / not live,
    400 unknown server, 409 the CLI refused the toggle."""
    if not get_settings().mcp_scope_enabled:
        return JSONResponse(status_code=404, content={"error": "mcp_scope_disabled"})
    if server not in await asyncio.to_thread(configured_server_keys):
        raise HTTPException(status_code=400, detail={"code": "UNKNOWN_SERVER"})
    try:
        return await session_actions.mcp_toggle(
            _get_orchestrator(request), db, session_id=session_id, server=server,
            enabled=body.enabled, identity=identity, via=getattr(request.state, "via", None))
    except ActionError as exc:
        return _action_error_response(exc)


@router.post("/{session_id}/permissions/{perm_id}")
async def resolve_session_permission(
    session_id: str,
    perm_id: str,
    body: PermissionResponseRequest,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
):
    """Answer a pending permission card (HTTP twin of the WS `permissionResponse`
    frame). 400 if the assistant token tries to grant `"always"`; 404 if the
    permission is no longer pending."""
    orch = _get_orchestrator(request)
    # "always" writes a standing rule, so its human-only gate must see a local process
    # as one (#2980); allow/deny keep the plain attribution and skip the /proc read.
    via = (human_gate_via(request) if body.decision == "always"
           else getattr(request.state, "via", None))
    try:
        return await session_actions.resolve_permission(
            orch, db, session_id=session_id, perm_id=perm_id,
            decision=body.decision, identity=identity, via=via)
    except ActionError as exc:
        return _action_error_response(exc)


@router.post("/{session_id}/questions/{question_id}")
async def answer_session_question(
    session_id: str,
    question_id: str,
    body: QuestionAnswerRequest,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
):
    """Answer a pending AskUserQuestion (HTTP twin of the WS `answerQuestion` frame).
    404 if the question is no longer pending."""
    orch = _get_orchestrator(request)
    via = getattr(request.state, "via", None)
    try:
        return await session_actions.answer_question(
            orch, db, session_id=session_id, question_id=question_id,
            selected=body.selected, mode=body.mode, identity=identity, via=via)
    except ActionError as exc:
        return _action_error_response(exc)


@router.post("/{session_id}/permission-mode")
async def set_session_permission_mode(
    session_id: str,
    body: PermissionModeRequest,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Change a session's permission mode (HTTP twin of the WS `setPermissionMode`
    frame)."""
    orch = _get_orchestrator(request)
    via = getattr(request.state, "via", None)
    return await session_actions.set_permission_mode(
        orch, db, session_id=session_id, mode=body.mode, identity=identity, via=via)


@router.post("/{session_id}/model")
async def set_session_model(
    session_id: str,
    body: SetModelRequest,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
):
    """Switch a session's model (→ `set_model_for_session`). 409 if the model is
    blocked by policy."""
    orch = _get_orchestrator(request)
    via = getattr(request.state, "via", None)
    try:
        return await session_actions.set_model(
            orch, db, session_id=session_id, model=body.model, identity=identity, via=via)
    except ActionError as exc:
        return _action_error_response(exc)


@router.post("/{session_id}/suggest")
async def generate_session_suggestions(
    session_id: str,
    request: Request,
    identity: str = Depends(current_identity),
) -> dict:
    """Generate fresh next-prompt suggestions for a session via the LLM, on demand.

    Backs the sparkle (✨) action: tapping it ALWAYS asks the model for a new
    suggestion rather than replaying a cached value or a hard-coded fallback (those
    live only in the dropdown). Resume-less and best-effort — never resumes the
    session merely to suggest, and returns an empty list rather than erroring when
    the model yields nothing. 404 only when the session itself is unknown."""
    orch = _get_orchestrator(request)
    record = await orch.get_session(session_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": f"Session {session_id!r} not found"},
        )
    suggestions = await orch.generate_suggestions_now(session_id)
    return {"suggestions": list(suggestions or [])}


@router.post("/{session_id}/fix-pr")
async def fix_session_pr(
    session_id: str,
    body: FixPrRequest,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Manual 'Fix PR #N' tap (propose mode): resume the owning session if
    detached and inject the same branch-aware fix prompt the auto path uses.

    Routes through orch.request_pr_fix so the manual and autonomous fixes are
    one implementation. The auto-mode loop guard does NOT apply here — a human
    tap is always allowed."""
    orch = _get_orchestrator(request)
    record = await orch.get_session(session_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": f"Session {session_id!r} not found"},
        )
    driver = await orch.ensure_driver(session_id, identity)
    if driver is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_resume_failed_detail(record),
        )
    result = await orch.request_pr_fix(session_id, body.number)
    if result != "sent":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": result, "message": f"Cannot fix PR #{body.number}: {result}"},
        )
    await record_audit(db, "session.fix_pr", identity,
                       {"sessionId": session_id, "number": body.number})
    updated = await orch.get_session(session_id) or record
    return _record_to_dict(updated, repo=await _resolve_repo(updated))


@router.post("/{session_id}/reload")
async def reload_session(
    session_id: str,
    request: Request,
    response: Response,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """"Reload session": rebuild the session's driver in place.

    The recovery a human reaches for when the UI can no longer act on a session
    (answers rejected, a turn that stopped streaming, a driver the daemon lost).
    Routes through orch.reload_session — the same driver-swap primitive rotation
    and crash recovery use — with the caller's identity threaded onto the new
    driver. 404 for an unknown session; 409 (code = the orchestrator's reason)
    when the session is parked somewhere a rebuild makes no sense (archived,
    queued, awaiting approval) or the rebuild left it driverless. 202 when the
    rebuild is still running past the daemon's answer window ("reloading"): it
    finishes in the background, and a prompt sent meanwhile waits for the new
    driver."""
    orch = _get_orchestrator(request)
    record = await orch.get_session(session_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": f"Session {session_id!r} not found"},
        )
    result = await orch.reload_session(session_id, identity)
    if result == "reloading":
        response.status_code = status.HTTP_202_ACCEPTED
    elif result != "reloaded":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": result, "message": f"Session could not be reloaded: {result}"},
        )
    await record_audit(db, "session.reload", identity, {"sessionId": session_id})
    updated = await orch.get_session(session_id) or record
    return _record_to_dict(updated, repo=await _resolve_repo(updated))


@router.post("/{session_id}/rotate")
async def rotate_session(
    session_id: str,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """"Next best account": move the session to the account the selector ranks
    best and continue the conversation (orch.rotate_to_best — the same driver-swap
    primitive rotation uses). 404 unknown; 409 (code = the orchestrator's reason)
    when nothing has headroom or the session is parked where a move makes no sense."""
    orch = _get_orchestrator(request)
    record = await orch.get_session(session_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": f"Session {session_id!r} not found"},
        )
    result = await orch.rotate_to_best(session_id, identity)
    if result not in ("rotated", "rotated_new_thread"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": result, "message": f"Session could not be rotated: {result}"},
        )
    await record_audit(db, "session.rotate", identity, {"sessionId": session_id})
    updated = await orch.get_session(session_id) or record
    return _record_to_dict(updated, repo=await _resolve_repo(updated))


class AccountPinRequest(BaseModel):
    pinned: bool


@router.post("/{session_id}/account-pin")
async def set_account_pin(
    session_id: str,
    body: AccountPinRequest,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Pin the session to its current account, or clear the pin (the 📌 button).
    Unpinning a `limited` session rotates it immediately."""
    orch = _get_orchestrator(request)
    record = await orch.get_session(session_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": f"Session {session_id!r} not found"},
        )
    await orch.set_account_pin(session_id, body.pinned)
    await record_audit(db, "session.account_pin", identity,
                       {"sessionId": session_id, "pinned": body.pinned})
    updated = await orch.get_session(session_id) or record
    return _record_to_dict(updated, repo=await _resolve_repo(updated))


@router.delete("/{session_id}/permanent", status_code=status.HTTP_204_NO_CONTENT)
async def permanent_delete_session(
    session_id: str,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
):
    """Permanently delete a session: remove archive folder and DB row. 404 if unknown."""
    orch = _get_orchestrator(request)
    deleted = await orch.permanent_delete(session_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": f"Session {session_id!r} not found"},
        )

    # Audit: session.permanent_delete
    await record_audit(
        db=db,
        type="session.permanent_delete",
        identity=identity,
        detail={"session_id": session_id},
    )


@router.get("/{session_id}/transcript")
async def get_session_transcript(
    session_id: str,
    request: Request,
    before: int | None = None,
    limit: int | None = None,
    _identity: str = Depends(current_identity),
) -> dict:
    """Read-only transcript events, always with earliestTurn/totalTurns metadata.

    - no params → the FULL transcript, sub-agent threads included (no silent cap)
    - `limit` alone → the last `limit` turns
    - `before` → a turn-ordinal window for lazy scrollback paging
    """
    from server.db import db_session as _db_session
    from server.db.models import Session as _DbSession
    from server.audit import redact_event
    from sqlalchemy import select as sa_select

    orch = _get_orchestrator(request)

    # Verify the session exists (in _records for active, or DB for archived).
    record = await orch.get_session(session_id)
    if record is None:
        async with _db_session() as _db:
            row = (await _db.execute(
                sa_select(_DbSession).where(_DbSession.id == session_id)
            )).scalars().first()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "not_found", "message": f"Session {session_id!r} not found"},
            )

    # In-process orchestrator yields event objects (.to_wire()); the remote/daemon
    # orchestrator returns already-serialised wire dicts. Handle both (#172).
    def _wire(e):
        return redact_event(e.to_wire() if hasattr(e, "to_wire") else e)

    if before is not None and limit is None:
        limit = 20  # classic scrollback page size when only a cursor is given
    window = await orch.transcript_window(session_id, before=before, limit=limit)
    if window is None:
        return {"events": [], "earliestTurn": 0, "totalTurns": 0}
    return {
        "events": [_wire(e) for e in window.events],
        "earliestTurn": window.earliest_turn,
        "totalTurns": window.total_turns,
    }


@router.get("/{session_id}/export.md")
async def export_session_markdown(
    session_id: str,
    request: Request,
    _identity: str = Depends(current_identity),
):
    """Download session transcript as a human-readable Markdown file.

    Engine-neutral: the replay below already speaks both engines, so the only thing
    that used to make this Claude-only was the existence gate — ``transcript_path``
    answers None for every Codex session by design, 404'ing the export before it
    reached that replay (#1277). ``has_transcript`` asks the same question of
    whichever engine owns the session.
    """
    from server.audit import redact_event
    from server.sessions.export import transcript_markdown, safe_filename
    from server.sessions.orchestrator import TRANSCRIPT_EXPORT_CEILING_S
    orch = _get_orchestrator(request)
    if not await orch.has_transcript(session_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"code": "not_found", "message": "No transcript"})
    record = await orch.get_session(session_id)
    title = (getattr(record, "name", None) or getattr(record, "title", None) or session_id) if record else session_id
    # Full windowed replay (limit=None): the export must never be silently
    # truncated to a trailing slice of the session (B1).
    #
    # A download is not the session-open critical path, so it gets the larger export
    # ceiling instead of the interactive one. And because has_transcript() above
    # already proved a transcript EXISTS, a None here can only mean the replay timed
    # out or failed — surface that as 504 rather than a 200 with an empty document,
    # which would look to the user like a successful export of an empty session.
    window = await orch.transcript_window(session_id, timeout_s=TRANSCRIPT_EXPORT_CEILING_S)
    if window is None:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail={"code": "transcript_timeout",
                    "message": "Transcript replay timed out; export not generated"},
        )
    events = window.events
    # In-process orchestrator yields event objects (.to_wire()); the remote/daemon
    # orchestrator returns already-serialised wire dicts. Handle both (#172).
    md = transcript_markdown(
        [redact_event(e.to_wire() if hasattr(e, "to_wire") else e) for e in events],
        title=str(title),
    )
    fname = safe_filename(f"{title}-{session_id}") + ".md"
    return Response(content=md, media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@router.get("/{session_id}/export.jsonl")
async def export_session_jsonl(
    session_id: str,
    request: Request,
    _identity: str = Depends(current_identity),
):
    """Download the session transcript as an unredacted machine-readable JSONL file.

    Two shapes, because the engines genuinely differ and pretending otherwise is the
    bug (#1277):

    * A session with a native transcript file (Claude) streams that file VERBATIM,
      byte for byte, exactly as before — the user's own data over an
      already-authenticated connection (deliberate exception to the redaction rule).
    * A session without one (Codex keeps its history inside the app-server) gets
      ViberPit's normalized replay events as NDJSON, led by a header record naming
      the format. It is NOT relabelled as Claude-native JSONL: the two schemas share
      no field names, so a consumer that assumed otherwise would silently read
      garbage instead of failing. See ``export.normalized_ndjson``.

    Resolution uses ``resolve_transcript_path`` rather than ``transcript_path``, so an
    ARCHIVED Claude session now streams its real file instead of 404ing — the
    in-memory-driver-state limitation this route used to document.
    """
    from server.sessions.export import normalized_ndjson, safe_filename
    from server.sessions.orchestrator import TRANSCRIPT_EXPORT_CEILING_S
    orch = _get_orchestrator(request)
    path = await orch.resolve_transcript_path(session_id)
    # A path resolved by the orchestrator is only meaningful on the host that
    # resolved it: in fleet mode this is a WORKER daemon's filesystem, and the edge
    # opening it would raise inside the streaming generator, after the response had
    # already begun — an unrecoverable truncated download rather than an error. So
    # the verbatim branch is taken only for a file this process can actually read;
    # everything else falls through to the (host-independent) replay below, which is
    # the same RPC the rest of this API already uses to read foreign transcripts.
    #
    # to_thread, not a bare stat: transcripts live on an sshfs mount in production, and
    # a stalled mount turns this into a multi-second block of the whole event loop
    # (#826, the same reason resolve_transcript_path itself is offloaded).
    if path and await asyncio.to_thread(os.path.exists, path):
        def _iter():
            with open(path, "rb") as f:
                while chunk := f.read(65536):
                    yield chunk

        fname = safe_filename(f"{session_id}") + ".jsonl"
        return StreamingResponse(_iter(), media_type="application/x-ndjson",
                                 headers={"Content-Disposition": f'attachment; filename="{fname}"'})

    if not await orch.has_transcript(session_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"code": "not_found", "message": "No transcript"})

    # Same ceiling and the same None-means-error reading as /export.md: has_transcript
    # already proved the history exists, so a None here is a timeout, not emptiness.
    window = await orch.transcript_window(session_id, timeout_s=TRANSCRIPT_EXPORT_CEILING_S)
    if window is None:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail={"code": "transcript_timeout",
                    "message": "Transcript replay timed out; export not generated"},
        )
    record = await orch.get_session(session_id)
    engine = getattr(record, "engine", None) if record else None
    body = normalized_ndjson(
        # In-process orchestrator yields event objects (.to_wire()); the remote/daemon
        # orchestrator returns already-serialised wire dicts. Handle both (#172).
        [e.to_wire() if hasattr(e, "to_wire") else e for e in window.events],
        session_id=session_id,
        engine=getattr(engine, "value", None) or str(engine or "unknown"),
        model=getattr(record, "model", None) if record else None,
    )
    fname = safe_filename(f"{session_id}") + ".events.jsonl"
    return Response(content=body, media_type="application/x-ndjson",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@router.post("/{session_id}/model-approval")
async def model_approval(
    session_id: str,
    body: ModelApprovalBody,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Approve or deny a parked model-approval hold for a session.

    ``body.model``, when given, is a human COUNTER-OFFER: approve the hold at this
    model instead of the parked one. Absent means approve exactly what was requested.
    Ignored when ``decision`` is ``"deny"`` — a denial rejects the hold outright, so
    there is no model to approve at.

    Returns the updated session record (200), 404 if unknown, 409 if no pending
    approval to resolve or the counter-offer is blocked by policy, 422 if the
    counter-offered model is unknown. Bad ``decision`` values yield 422 from the
    ``Literal`` constraint.
    """
    if body.model is not None:
        from server.sessions.model_catalog import is_known_alias, model_tier
        if not (is_known_alias(body.model) or model_tier(body.model)):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"code": "unknown_model", "message": f"unknown model: {body.model}"})
    orch = _get_orchestrator(request)
    try:
        rec = await orch.resolve_model_approval(
            session_id, body.decision, model=body.model, identity=identity)
    except KeyError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="session not found")
    except ModelBlockedError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "model_blocked", "message": f"{body.model} is blocked by policy"})
    except ValueError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="no pending model approval")
    return _record_to_dict(rec)
