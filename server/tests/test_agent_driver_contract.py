from __future__ import annotations

from unittest import mock

import pytest

from server.sessions.claude_driver import ClaudeAgentDriver
from server.sessions.engine import (
    AgentCapabilities,
    AgentDriver,
    AgentDriverConfig,
    AgentDriverRegistry,
    AgentEngine,
    AgentEngineUnavailable,
    InvalidAgentEngine,
    parse_agent_engine,
)
from server.sessions.permissions import PermissionRegistry


def _claude_driver() -> ClaudeAgentDriver:
    return ClaudeAgentDriver(
        "session-1",
        "account-1",
        "/tmp/project",
        "/tmp/config",
        mode="interactive",
        sdk_factory=lambda *args, **kwargs: None,
        registry=PermissionRegistry(),
        identity="person@example.com",
    )


def test_claude_adapter_satisfies_provider_neutral_driver_contract():
    driver = _claude_driver()

    assert isinstance(driver, AgentDriver)
    assert driver.engine is AgentEngine.CLAUDE_CODE
    assert driver.engine_session_id is None
    assert driver.capabilities == AgentCapabilities(
        models=True,
        interrupt=True,
        approvals=True,
        questions=True,
        adopt=True,
        self_model_switch=True,
        subagents=True,
        attachments=True,
        # Rotation composes the target account's config dir, re-trusts the project
        # and syncs credentials back — Claude-specific machinery, so the orchestrator
        # gates the rotation path on this rather than on `if engine == ...`.
        account_rotation=True,
        # The CLI enumerates its own built-ins, custom commands and skills through
        # `get_server_info()`. `vendor_skills` stays False: the CLI parses `/name`
        # itself, so text passthrough IS this engine's native dispatch (#1286).
        vendor_commands=True,
    )

    driver._engine_session_id = "native-1"
    assert driver.engine_session_id == "native-1"


def test_concrete_driver_rename_leaves_no_compatibility_class():
    from server.sessions import claude_driver

    assert not hasattr(claude_driver, "SessionDriver")


def test_engine_parser_is_strict_except_at_legacy_deserialization_boundaries():
    assert parse_agent_engine(AgentEngine.CODEX) is AgentEngine.CODEX
    assert parse_agent_engine("claude_code") is AgentEngine.CLAUDE_CODE
    assert parse_agent_engine(None, legacy_default=True) is AgentEngine.CLAUDE_CODE

    with pytest.raises(InvalidAgentEngine, match="required"):
        parse_agent_engine(None)
    with pytest.raises(InvalidAgentEngine, match="unknown"):
        parse_agent_engine("openai")


def test_registry_distinguishes_unknown_from_recognized_but_unavailable_engine():
    factory = mock.Mock()
    registry = AgentDriverRegistry({AgentEngine.CLAUDE_CODE: factory})

    assert registry.require("claude_code") is AgentEngine.CLAUDE_CODE
    with pytest.raises(AgentEngineUnavailable) as exc_info:
        registry.require("codex")
    assert exc_info.value.engine is AgentEngine.CODEX
    with pytest.raises(InvalidAgentEngine):
        registry.require("not-an-engine")


def test_registry_is_the_only_construction_dispatch():
    driver = _claude_driver()

    class _Factory:
        def create(self, config: AgentDriverConfig) -> AgentDriver:
            assert config.session_id == "session-1"
            return driver

    registry = AgentDriverRegistry({AgentEngine.CLAUDE_CODE: _Factory()})
    config = AgentDriverConfig(
        session_id="session-1",
        account="account-1",
        cwd="/tmp/project",
        config_dir="/tmp/config",
        mode="interactive",
        identity="person@example.com",
    )

    assert registry.create(AgentEngine.CLAUDE_CODE, config) is driver


@pytest.mark.asyncio
async def test_orchestrator_rejects_codex_before_launch_side_effects():
    from server.sessions.orchestrator import SessionOrchestrator

    orchestrator = SessionOrchestrator(sdk_factory=lambda *args, **kwargs: None)

    with pytest.raises(AgentEngineUnavailable) as exc_info:
        await orchestrator.create_session(name="Codex", engine="codex")

    assert exc_info.value.engine is AgentEngine.CODEX


def test_engine_addition_does_not_bump_the_global_daemon_protocol():
    from server.ipc.protocol import PROTOCOL_VERSION

    assert PROTOCOL_VERSION == 1
