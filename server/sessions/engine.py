"""Provider-neutral agent engine vocabulary and driver construction boundary."""
from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from server.assistant.context import PromptContext


class AgentEngine(StrEnum):
    """Native executable that owns a coding session."""

    CLAUDE_CODE = "claude_code"
    CODEX = "codex"


class InvalidAgentEngine(ValueError):
    """Raised when a request names no recognized native agent engine."""


class AgentEngineUnavailable(RuntimeError):
    """Raised when an engine is recognized but has no installed driver factory."""

    def __init__(self, engine: AgentEngine) -> None:
        self.engine = engine
        super().__init__(f"agent engine is not available: {engine.value}")


def parse_agent_engine(
    value: AgentEngine | str | None,
    *,
    legacy_default: bool = False,
) -> AgentEngine:
    """Parse an engine value, defaulting a missing value only on legacy boundaries."""
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


@dataclass(frozen=True, slots=True)
class AgentCapabilities:
    """Optional interaction surfaces exposed by one driver implementation."""

    models: bool = False
    reasoning_effort: bool = False
    steer: bool = False
    interrupt: bool = False
    approvals: bool = False
    questions: bool = False
    fork: bool = False
    adopt: bool = False
    self_model_switch: bool = False
    subagents: bool = False
    attachments: bool = False


@runtime_checkable
class AgentDriverStream(Protocol):
    turn_active: bool
    turn_started_at: float | None

    def events(self) -> AsyncIterator[object]: ...
    def close(self) -> None: ...


@runtime_checkable
class AgentDriver(Protocol):
    """Actual common runtime surface consumed by orchestration and WebSocket code."""

    session_id: str
    account: str
    cwd: str
    config_dir: str
    mode: str

    @property
    def engine(self) -> AgentEngine: ...
    @property
    def engine_session_id(self) -> str | None: ...
    @property
    def capabilities(self) -> AgentCapabilities: ...
    @property
    def identity(self) -> str: ...
    @identity.setter
    def identity(self, value: str) -> None: ...
    @property
    def turn_active(self) -> bool: ...
    @property
    def turn_started_at(self) -> float | None: ...
    @property
    def drain_alive(self) -> bool: ...
    @property
    def in_api_backoff(self) -> bool: ...
    @property
    def awaiting_user_input(self) -> bool: ...
    @property
    def last_progress_at(self) -> float: ...

    async def start(self) -> None: ...
    async def aclose(self) -> None: ...
    async def send_prompt(
        self,
        text: str,
        images: list[dict] | None = None,
        *,
        interrupt: bool = False,
        context: PromptContext | None = None,
    ) -> None: ...
    async def interrupt(self, *, keep_queue: bool = False) -> None: ...
    async def set_model(self, model: str | None) -> None: ...
    async def set_permission_mode(self, mode: str) -> None: ...
    def resolve_permission(self, perm_id: str, decision: str, identity: str) -> bool: ...
    def answer_question(
        self,
        question_id: str,
        selected: list[str],
        identity: str,
        mode: str,
    ) -> bool: ...
    def open_stream(self) -> AgentDriverStream: ...
    def events(self) -> AsyncIterator[object]: ...
    def current_status(self) -> str: ...
    def queued(self) -> list[str]: ...
    def queue_wire(self) -> list[dict]: ...
    async def cancel_queued(self, index: int) -> bool: ...
    async def emit_error(self, code: str, message: str) -> None: ...
    def emit(self, event: object) -> None: ...
    def pending_questions(self) -> list[dict]: ...
    def seed_idle_suggestions(self, user_text: str, assistant_text: str) -> None: ...
    async def server_info(self) -> dict | None: ...
    async def context_usage(self) -> dict: ...
    async def reconnect(self) -> None: ...


class SuggestionProvider(Protocol):
    async def generate(
        self,
        cwd: str,
        config_dir: str,
        context: str,
        account: str | None = None,
    ) -> list[str]: ...


class AsyncCloser(Protocol):
    async def aclose(self) -> None: ...


@dataclass(frozen=True, slots=True)
class AgentDriverConfig:
    """Engine-independent inputs needed to construct one live session driver."""

    session_id: str
    account: str
    cwd: str
    config_dir: str
    mode: str
    identity: str
    resume: str | None = None
    #: Reasoning effort requested at launch, for engines that expose the knob
    #: (`AgentCapabilities.reasoning_effort`). Already persisted on the session row
    #: and carried on `SessionRecord`; it reaches the driver here so an engine that
    #: sets it at thread-start time does not have to re-read the DB. Engines without
    #: the capability ignore it.
    reasoning_effort: str | None = None
    on_engine_session_id: Callable[[str, str], Awaitable[None]] | None = None
    on_sync_back: Callable[[], None] | None = None
    on_status: Callable[[str, str], Awaitable[None]] | None = None
    on_record_condense_ms: Callable[[float], Awaitable[None]] | None = None
    get_condense_estimate: Callable[[], Awaitable[float]] | None = None
    on_recoverable_failure: Callable[[str, str, float | None], Awaitable[None]] | None = None
    on_context_usage: Callable[[str, int, int], Awaitable[None]] | None = None
    clock: Callable[[], float] | None = None
    suggester: SuggestionProvider | None = None
    assistant_api: AsyncCloser | None = None


class AgentDriverFactory(Protocol):
    def create(self, config: AgentDriverConfig) -> AgentDriver: ...


class AgentDriverRegistry:
    """The sole engine-to-driver-factory registry."""

    def __init__(self, factories: Mapping[AgentEngine, AgentDriverFactory]) -> None:
        self._factories = dict(factories)

    @property
    def engines(self) -> Sequence[AgentEngine]:
        return tuple(self._factories)

    def require(self, engine: AgentEngine | str) -> AgentEngine:
        parsed = parse_agent_engine(engine)
        if parsed not in self._factories:
            raise AgentEngineUnavailable(parsed)
        return parsed

    def create(
        self,
        engine: AgentEngine | str,
        config: AgentDriverConfig,
    ) -> AgentDriver:
        parsed = self.require(engine)
        return self._factories[parsed].create(config)
