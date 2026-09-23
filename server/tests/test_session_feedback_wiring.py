"""Orchestrator/registry wiring for the session-feedback filer (#1434).

Mirrors test_error_triage_wiring: every seam an automation needs to actually tick
is cheap to forget and silent when missing — an unregistered filer has no card, a
loop absent from _PRIMARY_ONLY_LOOP_ATTRS runs on every worker daemon against one
shared DB, and a ctx-map entry left out pins the badge on "idle" forever.
"""
from __future__ import annotations

import inspect


def test_session_feedback_is_registered_with_oversight():
    from server.automations.registry import registry
    a = registry.get("session-feedback")
    assert a.id == "session-feedback" and a.modes == ("off", "file")
    assert set(registry.oversight_for("session-feedback").mode_descriptions) == {"off", "file"}


def test_config_schema_carries_the_spec_knobs():
    from server.automations.registry import registry
    schema = {f.key: f for f in registry.schema_for("session-feedback")
              if f.key in ("target_repo", "label", "daily_cap", "min_impact")}
    assert set(schema) == {"target_repo", "label", "daily_cap", "min_impact"}
    assert schema["daily_cap"].default == 20
    assert schema["min_impact"].choices == ["minor", "degraded", "blocked"]


def test_loop_is_primary_only_and_torn_down():
    from server.sessions import orchestrator as mod
    assert "_session_feedback_task" in mod._PRIMARY_ONLY_LOOP_ATTRS
    src = inspect.getsource(mod.SessionOrchestrator.stop)
    assert '"_session_feedback_task"' in src, "stop() must reap the session-feedback loop"


def test_has_action_provider_and_status_ctx():
    from server.sessions.orchestrator import SessionOrchestrator
    init_src = inspect.getsource(SessionOrchestrator.__init__)
    assert '"session-feedback": SessionFeedbackActionProvider(self)' in init_src
    statuses_src = inspect.getsource(SessionOrchestrator.automation_statuses)
    assert '"session-feedback": getattr(self, "_session_feedback_ctx", None)' in statuses_src


def test_read_config_resolves_mode_like_its_siblings(tmp_path):
    """Enabled with mode left NULL must resolve to `file`: a 2-mode automation has
    no mode <select>, so the toggle is the only thing that ever sets it."""
    import asyncio
    from server.db import db_session, init_db
    from server.db.models import Automation

    async def run():
        await init_db(f"sqlite+aiosqlite:///{tmp_path}/t.db")
        from server.sessions.orchestrator import SessionOrchestrator
        orch = SessionOrchestrator.__new__(SessionOrchestrator)
        mode, config = await SessionOrchestrator._session_feedback_read_config(orch)
        assert mode == "off" and config["daily_cap"] == 20
        async with db_session() as db:
            db.add(Automation(id="session-feedback", enabled=True, mode=None))
            await db.flush()
        mode, _ = await SessionOrchestrator._session_feedback_read_config(orch)
        assert mode == "file"
    asyncio.run(run())
