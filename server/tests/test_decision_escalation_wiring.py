from __future__ import annotations

import inspect


def test_decision_escalation_is_registered():
    from server.automations.registry import registry
    a = registry.get("decision-escalation")
    assert a.id == "decision-escalation" and a.modes == ("off", "notify")
    assert [f.key for f in a.config_schema] == ["tick_min", "starving_max_dispatchable",
                                                "hold_age_hours", "downstream_threshold",
                                                "repeat_hours", "products"]


def test_decision_escalation_loop_is_primary_only_and_torn_down():
    from server.sessions import orchestrator as mod
    assert "_decision_escalation_task" in mod._PRIMARY_ONLY_LOOP_ATTRS
    src = inspect.getsource(mod.SessionOrchestrator.stop)
    assert '"_decision_escalation_task"' in src, "stop() must reap the decision-escalation loop"
    start_src = inspect.getsource(mod.SessionOrchestrator.start)
    assert "decision-escalation-loop" in start_src


def test_decision_escalation_has_action_provider_and_status_ctx():
    from server.sessions.orchestrator import SessionOrchestrator
    init_src = inspect.getsource(SessionOrchestrator.__init__)
    assert '"decision-escalation": DecisionEscalationActionProvider(self)' in init_src
    statuses_src = inspect.getsource(SessionOrchestrator.automation_statuses)
    assert ('"decision-escalation": getattr(self, "_decision_escalation_ctx", None)'
            in statuses_src)


def test_decision_needed_is_a_notification_class_with_an_assistant_watch():
    from server.assistant.watches import CONDITION_MAP
    from server.sessions.notify_rules import EVENT_CLASSES
    assert "decision_needed" in EVENT_CLASSES
    assert CONDITION_MAP["product"]["needs_decision"] == ("decision_needed", "scope_automation")


def test_read_config_resolves_enabled_row_without_mode_to_notify(monkeypatch):
    import asyncio
    from server.sessions.orchestrator import SessionOrchestrator
    seen = {}

    async def fake_read(automation, *, modes, default_mode=None, log_key=None):
        seen.update(modes=modes, default_mode=default_mode, log_key=log_key)
        return default_mode, {}

    from server.automations import config_store
    monkeypatch.setattr(config_store, "read_automation_config", fake_read)
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    mode, _ = asyncio.run(SessionOrchestrator._decision_escalation_read_config(orch))
    assert mode == "notify" and seen["modes"] == ("notify",)


def test_web_notification_labels_include_decision_needed_and_account_limited():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    src = (root / "web/src/features/settings/sections/notificationsApi.ts").read_text()
    assert "{ value: 'decision_needed', label: 'Decision needed' }" in src
    assert "{ value: 'account_limited', label: 'Account limited' }" in src


def test_tick_interval_comes_from_config(monkeypatch):
    """``tick_min`` is config, not a start() literal: the loop sleeps what the row says."""
    import asyncio
    from server.sessions.orchestrator import SessionOrchestrator
    orch = SessionOrchestrator.__new__(SessionOrchestrator)

    async def fake_read():
        return "notify", {"tick_min": 7}
    orch._decision_escalation_read_config = fake_read  # type: ignore[attr-defined]
    assert asyncio.run(SessionOrchestrator._decision_escalation_interval(orch)) == 7 * 60.0
