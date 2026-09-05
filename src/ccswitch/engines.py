"""Provider-neutral native agent engine vocabulary.

This module lives in the installable core so both the CLI selectors and the
server driver registry consume one enum and one parser. Server code re-exports
these names from :mod:`server.sessions.engine` for compatibility.
"""
from __future__ import annotations

from enum import StrEnum


class AgentEngine(StrEnum):
    """Native executable that owns a coding session."""

    CLAUDE_CODE = "claude_code"
    CODEX = "codex"


class InvalidAgentEngine(ValueError):
    """Raised when a request names no recognized native agent engine."""


def parse_agent_engine(
    value: AgentEngine | str | None,
    *,
    legacy_default: bool = False,
) -> AgentEngine:
    """Parse an engine value, defaulting a missing value only at legacy boundaries."""
    if value is None:
        if legacy_default:
            return AgentEngine.CLAUDE_CODE
        raise InvalidAgentEngine("agent engine is required")
    if isinstance(value, AgentEngine):
        return value
    try:
        return AgentEngine(value)
    except (TypeError, ValueError) as exc:
        raise InvalidAgentEngine(f"unknown agent engine: {value!r}") from exc
