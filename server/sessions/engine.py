"""Provider-neutral agent engine vocabulary and driver construction boundary."""
from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ccswitch.engines import (
    AgentEngine,
    InvalidAgentEngine as InvalidAgentEngine,
    parse_agent_engine,
)
from server.assistant.context import PromptContext

class AgentEngineUnavailable(RuntimeError):
    """Raised when an engine is recognized but has no installed driver factory."""

    def __init__(self, engine: AgentEngine) -> None:
        self.engine = engine
        super().__init__(f"agent engine is not available: {engine.value}")


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
    #: The session can be moved to a DIFFERENT account under a live conversation
    #: and resumed there (usage-ceiling rotation, manual account switch, 401
    #: recompose). That machinery is credential-authority work — it composes the
    #: target account's config dir, re-trusts the project and syncs credentials
    #: back — so it is a property of the engine, not something every engine gets
    #: for free. An engine that leaves this False must be REFUSED by the rotation
    #: path rather than rebuilt as some other engine's driver.
    account_rotation: bool = False
    #: The session can be moved to a different account WITHOUT preserving the
    #: live conversation — the old thread is closed and a fresh one is started
    #: under the new account, on the SAME engine. This is the fallback for an
    #: engine that isolates accounts per-process (no shared credential-swap
    #: primitive to resume a thread under a different identity) but has no
    #: reason a brand-new thread can't start there instead. Distinct from
    #: ``account_rotation`` (True there means resume the SAME thread); an engine
    #: may declare either, both, or neither. _reassign tries account_rotation
    #: first and only falls back to this — never silently prefers a lossy switch
    #: over a lossless one.
    account_switch_new_thread: bool = False


def capabilities_to_wire(caps: AgentCapabilities) -> dict[str, bool]:
    """The browser shape of one capability set.

    THE serializer — the plan makes capabilities (not ``if engine === ...``) decide
    which controls a session surface renders, so every payload that carries them
    (``/api/config``, a Codex availability report) must speak the same camelCase
    keys. Derived from the dataclass fields rather than restated, so adding a
    capability cannot leave one payload a field short.
    """
    return {
        _to_camel(field): bool(getattr(caps, field))
        for field in AgentCapabilities.__dataclass_fields__
    }


def _to_camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part.title() for part in rest)


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

    def capabilities_for(self, engine: AgentEngine | str) -> AgentCapabilities | None:
        """Capabilities of the driver this registry WOULD build for *engine*.

        Lets orchestration ask "can this session be adopted / rotated?" without
        holding a live driver — the question is asked precisely on the paths that
        are about to build one. ``None`` means "unknown": either no factory is
        registered for the engine (it cannot run here at all) or the factory does
        not declare capabilities (a test double). Callers must treat ``None`` as
        NOT capable — an unknown engine getting the Claude-only treatment by
        default is exactly the mis-handling this exists to stop.

        The value is read off the factory, which publishes its driver class's own
        ``capabilities`` attribute rather than restating it, so there is still one
        declaration per engine.
        """
        parsed = parse_agent_engine(engine)
        factory = self._factories.get(parsed)
        caps = getattr(factory, "capabilities", None)
        return caps if isinstance(caps, AgentCapabilities) else None

    def create(
        self,
        engine: AgentEngine | str,
        config: AgentDriverConfig,
    ) -> AgentDriver:
        parsed = self.require(engine)
        return self._factories[parsed].create(config)
