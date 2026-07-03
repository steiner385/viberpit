"""Async facade over the in-process SessionOrchestrator.

Presents every edge-called operation as an async method so the edge can talk to
one uniform interface — satisfied here by the in-process orchestrator, and in
A2.2b by a remote proxy. The wrapped orchestrator is unchanged (still sync/async
as it is today); this layer only adds async-ness at the edge boundary.

Each method forwards to the orchestrator and `_maybe_await`s the result, so it is
correct whether the underlying method is sync OR async — no per-method
sync/async classification to get wrong.
"""
import inspect


async def _maybe_await(result):
    if inspect.isawaitable(result):
        return await result
    return result


class InProcessOrchestratorAdapter:
    def __init__(self, orch):
        self._orch = orch

    async def create_session(self, *a, **k): return await _maybe_await(self._orch.create_session(*a, **k))
    async def send_prompt(self, *a, **k): return await _maybe_await(self._orch.send_prompt(*a, **k))
    async def ensure_driver(self, *a, **k): return await _maybe_await(self._orch.ensure_driver(*a, **k))
    async def archive_session(self, *a, **k): return await _maybe_await(self._orch.archive_session(*a, **k))
    async def unarchive(self, *a, **k): return await _maybe_await(self._orch.unarchive(*a, **k))
    async def permanent_delete(self, *a, **k): return await _maybe_await(self._orch.permanent_delete(*a, **k))
    async def adopt_session(self, *a, **k): return await _maybe_await(self._orch.adopt_session(*a, **k))
    async def rename_session(self, *a, **k): return await _maybe_await(self._orch.rename_session(*a, **k))
    async def set_autopilot_paused(self, *a, **k): return await _maybe_await(self._orch.set_autopilot_paused(*a, **k))
    async def list_archived(self, *a, **k): return await _maybe_await(self._orch.list_archived(*a, **k))
    async def transcript_events(self, *a, **k): return await _maybe_await(self._orch.transcript_events(*a, **k))
    async def transcript_window(self, *a, **k): return await _maybe_await(self._orch.transcript_window(*a, **k))
    async def interrupt(self, *a, **k): return await _maybe_await(self._orch.interrupt(*a, **k))
    async def set_permission_mode(self, *a, **k): return await _maybe_await(self._orch.set_permission_mode(*a, **k))
    async def cancel_queued(self, *a, **k): return await _maybe_await(self._orch.cancel_queued(*a, **k))
    async def run_command(self, *a, **k): return await _maybe_await(self._orch.run_command(*a, **k))
    async def list_sessions(self, *a, **k): return await _maybe_await(self._orch.list_sessions(*a, **k))
    async def get_session(self, *a, **k): return await _maybe_await(self._orch.get_session(*a, **k))
    async def latest_suggestions(self, *a, **k): return await _maybe_await(self._orch.latest_suggestions(*a, **k))
    async def generate_suggestions_now(self, *a, **k): return await _maybe_await(self._orch.generate_suggestions_now(*a, **k))
    async def latest_summary(self, *a, **k): return await _maybe_await(self._orch.latest_summary(*a, **k))
    async def transcript_path(self, *a, **k): return await _maybe_await(self._orch.transcript_path(*a, **k))
    async def _resolve_transcript_path(self, *a, **k): return await _maybe_await(self._orch._resolve_transcript_path(*a, **k))
    async def pending_questions(self, *a, **k): return await _maybe_await(self._orch.pending_questions(*a, **k))
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
    async def all_repos_stage_models(self): return await _maybe_await(self._orch.all_repos_stage_models())
    async def request_pr_fix(self, *a, **k): return await _maybe_await(self._orch.request_pr_fix(*a, **k))
    async def automation_statuses(self, *a, **k): return await _maybe_await(self._orch.automation_statuses(*a, **k))
    async def pr_doctor_actionable(self, *a, **k): return await _maybe_await(self._orch.pr_doctor_actionable(*a, **k))
    async def pr_doctor_fix(self, *a, **k): return await _maybe_await(self._orch.pr_doctor_fix(*a, **k))
    async def backlog_runner_implement(self, *a, **k): return await _maybe_await(self._orch.backlog_runner_implement(*a, **k))
