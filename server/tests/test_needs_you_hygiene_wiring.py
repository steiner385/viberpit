"""Needs-you hygiene is registered, primary-only, torn down, and reports status."""
from __future__ import annotations

import asyncio
import inspect


def test_needs_you_hygiene_is_registered_with_oversight():
    from server.automations.registry import registry
    a = registry.get("needs-you-hygiene")
    assert a.modes == ("off", "dry_run", "on") and a.work_class == "maintenance"
    oversight = registry.oversight_for("needs-you-hygiene")
    assert oversight is not None
    assert {r.target_id for r in oversight.relationships} >= {"gh-reconcile", "backlog-runner"}


def test_needs_you_hygiene_loop_is_primary_only_and_torn_down():
    from server.sessions import orchestrator as mod
    assert "_needs_you_hygiene_task" in mod._PRIMARY_ONLY_LOOP_ATTRS
    assert '"_needs_you_hygiene_task"' in inspect.getsource(mod.SessionOrchestrator.stop)
    assert "needs-you-hygiene-loop" in inspect.getsource(mod.SessionOrchestrator.start)
    statuses_src = inspect.getsource(mod.SessionOrchestrator.automation_statuses)
    assert '"needs-you-hygiene": getattr(self, "_needs_you_hygiene_ctx", None)' in statuses_src


def test_read_config_resolves_an_enabled_row_without_mode_to_on(monkeypatch):
    from server.automations import config_store
    from server.sessions.orchestrator import SessionOrchestrator
    seen: dict = {}

    async def fake_read(automation, *, modes, default_mode=None, log_key=None):
        seen.update(modes=modes, default_mode=default_mode)
        return default_mode, {}

    monkeypatch.setattr(config_store, "read_automation_config", fake_read)
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    mode, _ = asyncio.run(SessionOrchestrator._needs_you_hygiene_read_config(orch))
    assert mode == "on" and seen["modes"] == ("dry_run", "on")


def test_the_loop_applies_through_the_orchestrators_issue_seams():
    """The executor reaches the orchestrator's OWN issue writers under the facade's
    public names, so the loop and ``POST /api/pm/recommendations/apply`` (which hands
    it ``app.state.sessions``) run the same code."""
    from server.sessions.facade import InProcessOrchestratorAdapter
    from server.sessions.orchestrator import SessionOrchestrator
    from server.sessions.remote import RemoteOrchestrator
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    seams = SessionOrchestrator._hygiene_seams(orch)
    for name in ("pm_issue_close", "pm_board_status", "pm_remove_dependency"):
        assert getattr(seams, name).__func__ is getattr(SessionOrchestrator, f"_{name}")
        assert hasattr(InProcessOrchestratorAdapter, name)
        assert hasattr(RemoteOrchestrator, name)


def test_the_loop_assesses_close_outs_through_the_orchestrators_gh_seam(monkeypatch):
    """Design §3: the loop's assessor is the orchestrator's, which hands the
    assessor its own ``gh_run`` seam and context, with the engine-safe helper
    identity — so tests (and the runner's fakes) govern every GitHub read."""
    from server.automations import closeout_assessor
    from server.sessions import helper_call
    from server.sessions import orchestrator as mod
    assert "assess=self._needs_you_hygiene_assess" in inspect.getsource(
        mod.SessionOrchestrator.__init__)
    seen: dict = {}

    async def fake(ctx, row, *, gh_run, _config_dir=None, _query_fn=None):
        seen.update(ctx=ctx, row=row, gh_run=gh_run, cfg=_config_dir)
        return {"decision": None}
    monkeypatch.setattr(closeout_assessor, "assess_and_record", fake)
    monkeypatch.setattr(helper_call, "pick_helper_config_dir", lambda: "/cfg/helper")
    orch = mod.SessionOrchestrator.__new__(mod.SessionOrchestrator)
    orch._gh_run = gh = object()
    orch._needs_you_hygiene_ctx = ctx = object()
    assert asyncio.run(mod.SessionOrchestrator._needs_you_hygiene_assess(orch, "row")) == {
        "decision": None}
    assert seen == {"ctx": ctx, "row": "row", "gh_run": gh, "cfg": "/cfg/helper"}
