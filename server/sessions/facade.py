"""Async facade over the in-process SessionOrchestrator.

Presents every edge-called operation as an async method so the edge can talk to
one uniform interface — satisfied here by the in-process orchestrator, and in
A2.2b by a remote proxy. The wrapped orchestrator is unchanged (still sync/async
as it is today); this layer only adds async-ness at the edge boundary.

Each method forwards to the orchestrator and `_maybe_await`s the result, so it is
correct whether the underlying method is sync OR async — no per-method
sync/async classification to get wrong.

`call_with_deadline` is the matching helper for the other end of that boundary: it
bounds ONE edge→orchestrator call without ever cancelling an in-flight daemon RPC,
so callers need no per-method native-deadline classification either.
"""
import asyncio
import inspect

from server.sessions.engine import parse_agent_engine


async def _maybe_await(result):
    if inspect.isawaitable(result):
        return await result
    return result


# Attribute stamped by @rpc_deadline. Not the parameter NAME `timeout`: matching on
# that would silently mis-fire the day an unrelated method grows a `timeout=` meaning
# something else (a poll interval, a subprocess grace) — call_with_deadline would pass
# the RPC deadline in as that unrelated argument AND drop the wait_for fallback, leaving
# the call both mis-parameterised and unbounded. Exactly the shape of bug this module
# exists to remove, so the capability is declared explicitly instead of inferred.
_DEADLINE_ATTR = "__ccswitch_rpc_deadline__"


def rpc_deadline(fn):
    """Declare that *fn* enforces a caller's ``timeout=`` itself (it forwards it to
    ``DaemonClient.call``, i.e. to httpx), so ``call_with_deadline`` may hand the
    deadline to it instead of racing it from outside.

    Marking a method is a promise about what ``timeout=`` MEANS on it. The marked set is
    pinned by ``test_ipc_cancel_does_not_strand.py`` against an explicit allowlist, in
    both directions: adding or removing a marker, or adding an RPC that takes ``timeout``
    without one, fails that test rather than silently changing how deadlines are applied.
    """
    setattr(fn, _DEADLINE_ATTR, True)
    return fn


def _supports_native_deadline(fn) -> bool:
    """True when *fn* is marked with :func:`rpc_deadline`. Unmarked callees — this
    adapter's ``*a, **k`` forwarders, sync in-process methods, test doubles — take the
    ``wait_for`` fallback, which is safe because they own no socket."""
    # getattr on a bound method falls through to the underlying function's attributes.
    return getattr(fn, _DEADLINE_ATTR, False) is True


async def call_with_deadline(fn, *args, timeout: float, **kwargs):
    """Await ``fn(*args, **kwargs)`` under *timeout*, never cancelling a daemon RPC.

    ``asyncio.wait_for`` does not time an HTTP request out — it **cancels** it, throwing a
    ``CancelledError`` (a ``BaseException``) into httpcore. Measured consequence: the
    socket is orphaned for the life of the process and its pool slot is never reclaimed;
    once ``max_connections`` are stranded, every daemon-backed route 503s after exactly the
    pool timeout. That is the edge wedge of 2026-08-23 (issue #734); see
    ``docs/superpowers/spikes/2026-08-23-edge-socket-leak.md``, whose control showed that
    the same requests killed by httpx's OWN timeout leaked zero connections.

    The spike established THAT cancellation does this, not WHERE inside httpcore — its
    original ``AnyIOStream.start_tls`` explanation does not survive source reading, and the
    only shape that does (a pooled connection created but never dispatched on) involves no
    TLS at all. So do not reason from a TLS-specific mechanism to "the unix-socket branch is
    safe": that inference is unsupported, and this ban applies to both branches.

    So when the callee is marked :func:`rpc_deadline` — every ``RemoteOrchestrator`` RPC
    reached from here is, and it forwards the deadline to ``DaemonClient.call`` — hand the
    deadline to it and let httpx enforce it: httpx raises a plain ``Exception``, which is
    the path the spike's control showed cleans up. Expiry then surfaces as
    :class:`~server.ipc.client.DaemonTimeout` (a ``DaemonUnavailable``) rather than
    ``asyncio.TimeoutError``.

    Only fall back to ``asyncio.wait_for`` for an unmarked callee (the in-process adapter,
    test doubles), where the coroutine owns no socket and cancelling it is harmless. A callee that returns a non-awaitable is passed through unchanged,
    matching ``_maybe_await``'s tolerance of sync orchestrator methods.
    """
    if _supports_native_deadline(fn):
        return await _maybe_await(fn(*args, timeout=timeout, **kwargs))
    result = fn(*args, **kwargs)
    if not inspect.isawaitable(result):
        return result
    return await asyncio.wait_for(result, timeout)


class InProcessOrchestratorAdapter:
    def __init__(self, orch):
        self._orch = orch

    async def create_session(self, *a, **k):
        # In-process is a single host — 'prefer' is a fleet-routing hint that does
        # not apply here. Pop it before forwarding so the underlying orchestrator
        # (which does not accept prefer=) never sees it. (Ruling D)
        k.pop("prefer", None)
        if "engine" in k:
            engine = parse_agent_engine(k["engine"])
            k["engine"] = engine
        return await _maybe_await(self._orch.create_session(*a, **k))
    async def send_prompt(self, *a, **k): return await _maybe_await(self._orch.send_prompt(*a, **k))
    async def ensure_driver(self, *a, **k): return await _maybe_await(self._orch.ensure_driver(*a, **k))
    # Local/in-process mode has no daemon replay ring, so the wave-3 cursor catch-up never
    # applies here — always report "not covered" so ws.py takes the full disk-replay path
    # (the constant answer that keeps the two modes' attach code identical, constitution VI).
    async def ring_covers(self, session_id, since): return (False, None)
    # ring_covers is a constant False here, so ws.py's _maybe_catch_up returns before ever
    # reaching the ring_tail commit-time fetch — this exists for interface parity with the
    # remote orchestrator (and to keep the union type honest) and reports not-covered too.
    async def ring_tail(self, session_id, since): return {"covered": False, "events": [], "head": -1}
    # Local/in-process mode has no session daemon, so there is no process epoch to tag a
    # catch-up cursor with. None here means ws.py's epoch gate never matches → full replay,
    # which is moot anyway since ring_covers is a constant False (#497).
    def daemon_epoch(self): return None
    def daemon_sha(self): return None
    # In-process mode makes no HTTP calls to a daemon, so there is no RPC pool that could
    # wedge. None (not an empty snapshot) so the health surface says "not applicable"
    # rather than reporting a permanently-idle pool that does not exist.
    def daemon_pool_stats(self): return None
    async def archive_session(self, *a, **k): return await _maybe_await(self._orch.archive_session(*a, **k))
    async def unarchive(self, *a, **k): return await _maybe_await(self._orch.unarchive(*a, **k))
    async def permanent_delete(self, *a, **k): return await _maybe_await(self._orch.permanent_delete(*a, **k))
    async def adopt_session(self, *a, **k): return await _maybe_await(self._orch.adopt_session(*a, **k))
    async def rename_session(self, *a, **k): return await _maybe_await(self._orch.rename_session(*a, **k))
    async def note_user_interaction(self, *a, **k): return await _maybe_await(self._orch.note_user_interaction(*a, **k))
    async def set_autopilot_paused(self, *a, **k): return await _maybe_await(self._orch.set_autopilot_paused(*a, **k))
    async def list_archived(self, *a, **k): return await _maybe_await(self._orch.list_archived(*a, **k))
    async def transcript_events(self, *a, **k): return await _maybe_await(self._orch.transcript_events(*a, **k))
    async def transcript_window(self, *a, **k): return await _maybe_await(self._orch.transcript_window(*a, **k))
    # The attach-path replay for an engine with no transcript file (Codex). ws.py
    # falls back to it when transcript_path() is None; see the orchestrator method.
    async def engine_replay_window(self, *a, **k): return await _maybe_await(self._orch.engine_replay_window(*a, **k))
    async def interrupt(self, *a, **k): return await _maybe_await(self._orch.interrupt(*a, **k))
    # "Open a browser" — the in-process route (server/app.py) calls this the same way
    # the daemon RPC calls orchestrator.open_browser directly; the native-vs-virtual
    # branch lives once on SessionOrchestrator (Task 6). Deliberately NOT added to
    # RemoteOrchestrator — remote mode keeps using x11_open_browser.
    async def open_browser(self, *a, **k): return await _maybe_await(self._orch.open_browser(*a, **k))
    async def set_permission_mode(self, *a, **k): return await _maybe_await(self._orch.set_permission_mode(*a, **k))
    async def set_model_for_session(self, *a, **k): return await _maybe_await(self._orch.set_model_for_session(*a, **k))
    async def cancel_queued(self, *a, **k): return await _maybe_await(self._orch.cancel_queued(*a, **k))
    async def session_queue_move(self, *a, **k): return await _maybe_await(self._orch.session_queue_move(*a, **k))
    async def session_queue_cancel(self, *a, **k): return await _maybe_await(self._orch.session_queue_cancel(*a, **k))
    async def session_queue_positions(self, *a, **k): return await _maybe_await(self._orch.session_queue_positions(*a, **k))
    async def maybe_start_queued(self, *a, **k): return await _maybe_await(self._orch.maybe_start_queued(*a, **k))
    async def run_command(self, *a, **k): return await _maybe_await(self._orch.run_command(*a, **k))
    async def list_sessions(self, *a, **k): return await _maybe_await(self._orch.list_sessions(*a, **k))
    async def get_session(self, *a, **k): return await _maybe_await(self._orch.get_session(*a, **k))
    async def find_assistant(self, *a, **k): return await _maybe_await(self._orch.find_assistant(*a, **k))
    async def post_notice(self, *a, **k): return await _maybe_await(self._orch.post_notice(*a, **k))
    async def pop_pending_notices(self, *a, **k): return await _maybe_await(self._orch.pop_pending_notices(*a, **k))
    async def latest_suggestions(self, *a, **k): return await _maybe_await(self._orch.latest_suggestions(*a, **k))
    async def generate_suggestions_now(self, *a, **k): return await _maybe_await(self._orch.generate_suggestions_now(*a, **k))
    async def latest_summary(self, *a, **k): return await _maybe_await(self._orch.latest_summary(*a, **k))
    async def prs_for_many(self, *a, **k): return await _maybe_await(self._orch.prs_for_many(*a, **k))
    async def latest_suggestions_many(self, *a, **k): return await _maybe_await(self._orch.latest_suggestions_many(*a, **k))
    async def latest_summary_many(self, *a, **k): return await _maybe_await(self._orch.latest_summary_many(*a, **k))
    async def resume_interrupted(self, *a, **k): return await _maybe_await(self._orch.resume_interrupted(*a, **k))
    async def reload_session(self, *a, **k): return await _maybe_await(self._orch.reload_session(*a, **k))
    async def rotate_to_best(self, *a, **k): return await _maybe_await(self._orch.rotate_to_best(*a, **k))
    async def set_account_pin(self, *a, **k): return await _maybe_await(self._orch.set_account_pin(*a, **k))
    async def transcript_path(self, *a, **k): return await _maybe_await(self._orch.transcript_path(*a, **k))
    async def resolve_transcript_path(self, *a, **k): return await _maybe_await(self._orch.resolve_transcript_path(*a, **k))
    # Engine-neutral "is there history?" — the export routes' 404-vs-504 gate, which
    # transcript_path cannot answer for an engine with no transcript file.
    async def has_transcript(self, *a, **k): return await _maybe_await(self._orch.has_transcript(*a, **k))
    async def transcript_search_text(self, *a, **k): return await _maybe_await(self._orch.transcript_search_text(*a, **k))
    async def pending_questions(self, *a, **k): return await _maybe_await(self._orch.pending_questions(*a, **k))
    async def orphaned_interaction(self, *a, **k): return await _maybe_await(self._orch.orphaned_interaction(*a, **k))
    async def pop_pending_initial_prompt(self, *a, **k): return await _maybe_await(self._orch.pop_pending_initial_prompt(*a, **k))
    async def pop_pending_initial_attachment_ids(self, *a, **k): return await _maybe_await(self._orch.pop_pending_initial_attachment_ids(*a, **k))
    async def resolve_permission(self, *a, **k): return await _maybe_await(self._orch.resolve_permission(*a, **k))
    async def answer_question(self, *a, **k): return await _maybe_await(self._orch.answer_question(*a, **k))
    async def command_catalog(self, *a, **k): return await _maybe_await(self._orch.command_catalog(*a, **k))
    async def subscribe_events(self, *a, **k): return await _maybe_await(self._orch.subscribe_events(*a, **k))
    async def unsubscribe_events(self, *a, **k): return await _maybe_await(self._orch.unsubscribe_events(*a, **k))

    # --- edge-boundary helpers that hide orchestrator internals ---
    async def usage(self, name): return await _maybe_await(self._orch.poller.get(name))
    async def pending_permissions(self, session_id): return await _maybe_await(self._orch._registry.pending(session_id))
    async def active_session_count(self): return await _maybe_await(self._orch.active_session_count())
    async def live_session_load(self): return await _maybe_await(self._orch.live_session_load())
    async def live_session_counts(self): return await _maybe_await(self._orch.live_session_counts())
    def pr_for(self, repo, branch): return self._orch.pr_for(repo, branch)
    async def all_prs(self): return await _maybe_await(self._orch.all_prs())
    async def pr_detail(self, *a, **k): return await _maybe_await(self._orch.pr_detail(*a, **k))
    async def all_repos_stage_models(self): return await _maybe_await(self._orch.all_repos_stage_models())
    async def request_pr_fix(self, *a, **k): return await _maybe_await(self._orch.request_pr_fix(*a, **k))
    async def automation_statuses(self, *a, **k): return await _maybe_await(self._orch.automation_statuses(*a, **k))
    async def resume_recovery_status(self, *a, **k): return await _maybe_await(self._orch.resume_recovery_status(*a, **k))
    async def startup_report(self, *a, **k): return await _maybe_await(self._orch.startup_report(*a, **k))
    async def automation_actionable(self, *a, **k): return await _maybe_await(self._orch.automation_actionable(*a, **k))
    async def automation_trigger(self, *a, **k): return await _maybe_await(self._orch.automation_trigger(*a, **k))
    async def pr_doctor_job(self, *a, **k): return await _maybe_await(self._orch.pr_doctor_job(*a, **k))
    async def copilot_add_start(self, *a, **k): return await _maybe_await(self._orch.copilot_add_start(*a, **k))
    async def copilot_add_status(self, *a, **k): return await _maybe_await(self._orch.copilot_add_status(*a, **k))
    async def copilot_add_cancel(self, *a, **k): return await _maybe_await(self._orch.copilot_add_cancel(*a, **k))
    async def copilot_usage(self, *a, **k): return await _maybe_await(self._orch.copilot_usage(*a, **k))
    # --- codex (OpenAI) account surfaces: answered by the host that owns the app-server ---
    async def codex_models(self, *a, **k): return await _maybe_await(self._orch.codex_models(*a, **k))
    async def codex_account_status(self, *a, **k): return await _maybe_await(self._orch.codex_account_status(*a, **k))
    async def codex_validate_model(self, *a, **k): return await _maybe_await(self._orch.codex_validate_model(*a, **k))
    async def codex_invalidate_credentials(self, *a, **k): return await _maybe_await(self._orch.codex_invalidate_credentials(*a, **k))
    async def refine_prompt_codex(self, *a, **k): return await _maybe_await(self._orch.refine_prompt_codex(*a, **k))
    # --- pipeline / merge-queue methods (GET /api/pipeline/events and siblings) ---
    async def latest_queue_entries(self, *a, **k): return await _maybe_await(self._orch.latest_queue_entries(*a, **k))
    async def all_merge_trains(self, *a, **k): return await _maybe_await(self._orch.all_merge_trains(*a, **k))
    async def check_insights(self, *a, **k): return await _maybe_await(self._orch.check_insights(*a, **k))
    async def ready_merge(self, *a, **k): return await _maybe_await(self._orch.ready_merge(*a, **k))
    async def deploy_config_list(self, *a, **k): return await _maybe_await(self._orch.deploy_config_list(*a, **k))
    async def deploy_config_put(self, *a, **k): return await _maybe_await(self._orch.deploy_config_put(*a, **k))
    async def deploy_status(self, *a, **k): return await _maybe_await(self._orch.deploy_status(*a, **k))
    # --- git credential-refresh alerts (#670) ---
    async def git_credential_alerts(self, *a, **k): return await _maybe_await(self._orch.git_credential_alerts(*a, **k))
    async def dismiss_git_credential_alert(self, *a, **k): return await _maybe_await(self._orch.dismiss_git_credential_alert(*a, **k))
    # --- cross-host inspection RPCs (#709): run on the daemon host, never the edge ---
    async def list_adoptable_candidates(self, *a, **k): return await _maybe_await(self._orch.list_adoptable_candidates(*a, **k))
    async def migrate_archive_home(self, *a, **k): return await _maybe_await(self._orch.migrate_archive_home(*a, **k))
    async def cloned_repos(self, *a, **k): return await _maybe_await(self._orch.cloned_repos(*a, **k))
    # --- automation consolidation methods ---
    async def consolidation_candidates(self, *a, **k): return await _maybe_await(self._orch.consolidation_candidates(*a, **k))
    async def automation_consolidate(self, *a, **k): return await _maybe_await(self._orch.automation_consolidate(*a, **k))
    # --- backlog runner manual dispatch ---
    async def backlog_candidates(self, *a, **k): return await _maybe_await(self._orch.backlog_candidates(*a, **k))
    async def backlog_runner_dispatch(self, *a, **k): return await _maybe_await(self._orch.backlog_runner_dispatch(*a, **k))
    async def resolve_model_approval(self, *a, **k): return await _maybe_await(self._orch.resolve_model_approval(*a, **k))
    # --- pm onboarding seams (phase 2, 2026-08-31) ---
    async def pm_onboarding_counts(self, *a, **k): return await _maybe_await(self._orch.pm_onboarding_counts(*a, **k))
    async def pm_onboard_spawn_map(self, *a, **k): return await _maybe_await(self._orch.pm_onboard_spawn_map(*a, **k))
    async def pm_classify_run(self, *a, **k): return await _maybe_await(self._orch.pm_classify_run(*a, **k))
    async def pm_advance_map_merged(self, *a, **k): return await _maybe_await(self._orch.pm_advance_map_merged(*a, **k))
