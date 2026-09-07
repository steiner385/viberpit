from __future__ import annotations


def test_error_triage_is_registered():
    from server.automations.registry import registry
    a = registry.get("error-triage")
    assert a.id == "error-triage" and a.modes == ("off", "file")


def test_error_triage_loop_is_primary_only_and_torn_down():
    import inspect
    from server.sessions import orchestrator as mod
    assert "_error_triage_task" in mod._PRIMARY_ONLY_LOOP_ATTRS
    src = inspect.getsource(mod.SessionOrchestrator.stop)
    assert '"_error_triage_task"' in src, "stop() must reap the error-triage loop"


def test_error_triage_has_action_provider_and_status_ctx():
    from server.sessions.orchestrator import SessionOrchestrator
    import inspect
    init_src = inspect.getsource(SessionOrchestrator.__init__)
    assert '"error-triage": ErrorTriageActionProvider(self)' in init_src
    statuses_src = inspect.getsource(SessionOrchestrator.automation_statuses)
    assert '"error-triage": getattr(self, "_error_triage_ctx", None)' in statuses_src
