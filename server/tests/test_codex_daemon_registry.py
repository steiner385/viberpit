"""Async daemon startup must install the same engine it advertises."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.ipc import serve
from server.sessions.engine import AgentEngine, AgentEngineUnavailable
from server.sessions.orchestrator import SessionOrchestrator


@pytest.mark.asyncio
@pytest.mark.parametrize("available", [True, False])
async def test_daemon_registers_codex_before_rehydrate(monkeypatch, available):
    orch = SessionOrchestrator()
    manager = SimpleNamespace(sweep_orphans=lambda: 0, stop_all=AsyncMock()) if available else None
    monkeypatch.setattr("server.codex.manager.build_codex_manager", AsyncMock(return_value=manager))
    monkeypatch.setattr(serve, "init_db", AsyncMock())
    monkeypatch.setattr("server.codex.doctor.run_and_log", AsyncMock())
    monkeypatch.setattr(serve, "install_drain_on_sigterm", lambda _: lambda: None)

    async def rehydrate():
        assert orch._engine_registry.require(AgentEngine.CLAUDE_CODE)
        if available:
            assert orch._engine_registry.require(AgentEngine.CODEX) == AgentEngine.CODEX
        else:
            with pytest.raises(AgentEngineUnavailable):
                orch._engine_registry.require(AgentEngine.CODEX)

    monkeypatch.setattr(orch, "rehydrate", rehydrate)
    for method in ("rearm_background_tasks", "auto_resume_interrupted", "start", "stop"):
        monkeypatch.setattr(orch, method, AsyncMock())
    app = SimpleNamespace(state=SimpleNamespace())
    async with serve.daemon_lifespan(app, orch):
        # rehydrate failures are swallowed by startup; assert at the serving boundary too.
        await rehydrate()
