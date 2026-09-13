# server/tests/test_deploy_monitor_wiring.py
from __future__ import annotations

import inspect


def test_deploy_monitor_is_registered():
    from server.automations.registry import registry
    a = registry.get("deploy-monitor")
    assert a.id == "deploy-monitor" and a.modes == ("off", "watch", "file")


def test_deploy_monitor_loop_is_primary_only_and_torn_down():
    from server.sessions import orchestrator as mod
    assert "_deploy_monitor_task" in mod._PRIMARY_ONLY_LOOP_ATTRS
    src = inspect.getsource(mod.SessionOrchestrator.stop)
    assert '"_deploy_monitor_task"' in src, "stop() must reap the deploy-monitor loop"
    start_src = inspect.getsource(mod.SessionOrchestrator.start)
    assert "deploy-monitor-loop" in start_src


def test_deploy_monitor_has_action_provider_and_status_ctx():
    from server.sessions.orchestrator import SessionOrchestrator
    init_src = inspect.getsource(SessionOrchestrator.__init__)
    assert '"deploy-monitor": DeployMonitorActionProvider(self)' in init_src
    assert "fetch_deployments" in init_src
    statuses_src = inspect.getsource(SessionOrchestrator.automation_statuses)
    assert '"deploy-monitor": getattr(self, "_deploy_monitor_ctx", None)' in statuses_src


def test_deploy_failed_is_a_notification_class_with_an_assistant_watch():
    from server.assistant.watches import CONDITION_MAP
    from server.sessions.notify_rules import EVENT_CLASSES
    assert "deploy_failed" in EVENT_CLASSES
    assert CONDITION_MAP["deploy"]["fails"] == ("deploy_failed", "scope_repo")


def test_read_config_resolves_enabled_row_without_mode_to_watch(monkeypatch):
    import asyncio
    from server.sessions.orchestrator import SessionOrchestrator
    seen = {}

    async def fake_read(automation, *, modes, default_mode=None, log_key=None):
        seen.update(modes=modes, default_mode=default_mode, log_key=log_key)
        return default_mode, {}

    from server.automations import config_store
    monkeypatch.setattr(config_store, "read_automation_config", fake_read)
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    mode, _ = asyncio.run(SessionOrchestrator._deploy_monitor_read_config(orch))
    assert mode == "watch" and seen["modes"] == ("watch", "file")


def test_web_notification_labels_include_deploy_failed():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    src = (root / "web/src/features/settings/sections/NotificationsSection.tsx").read_text()
    assert "{ value: 'deploy_failed', label: 'Deploy failed' }" in src
