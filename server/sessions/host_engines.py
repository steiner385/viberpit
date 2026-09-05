"""What engines a runner advertises, and whether the edge may dispatch to it.

One module because there are three consumers of the same question and they must
never answer it differently:

* :func:`build_engines_info` — the DAEMON side, composing ``/info.engines``.
* :func:`parse_engines_info` — the EDGE side, reading that payload (the fleet
  probe persists it; :class:`~server.sessions.remote.RemoteOrchestrator` reads it
  live before it hands a non-Claude launch to a daemon).
* :func:`engine_dispatchable` — the single verdict function both the candidate
  filter and the pre-dispatch gate call.

**Rolling compatibility.** ``/info`` gained ``engines`` in this change, so for one
fleet-upgrade window an absent key means "legacy daemon: Claude only". That is the
plan's stated compatibility default and the ONLY place it is applied — an empty
``engines`` dict is *not* the same thing and means "this daemon told us it offers
nothing".

**Codex failure is isolated.** Nothing here can make a host unhealthy. A runner
with a missing Codex binary, a drifted app-server contract or external auth turned
off is a perfectly good Claude runner; it is only removed from the candidate list
of a launch that explicitly asked for Codex.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Mapping

from ccswitch.engines import AgentEngine

__all__ = [
    "ENGINES_INFO_KEY",
    "EngineAvailability",
    "LEGACY_ENGINES",
    "build_engines_info",
    "parse_engines_info",
    "engine_dispatchable",
    "engines_to_wire",
    "expected_contract_sha256",
]

#: The ``/info`` key. Named once so the daemon writer and the edge reader cannot drift.
ENGINES_INFO_KEY: Final = "engines"


@dataclass(frozen=True, slots=True)
class EngineAvailability:
    """One engine's advertised state on one runner.

    ``contract_sha256`` is the app-server schema contract the runner actually
    observed. The edge compares it against its OWN pin rather than trusting the
    runner's verdict: a runner that upgraded past the edge is exactly the mixed
    version rollout this field exists to catch.
    """

    engine: AgentEngine
    available: bool
    #: The runner's own typed reason (``CodexUnavailableReason``), or None when
    #: available. Deliberately the ONLY explanatory field: ``CodexAvailability.detail``
    #: is scrubbed to a fixed ``[REDACTED]`` on construction by design, so shipping it
    #: here would put that literal in front of an operator instead of a cause.
    reason: str | None = None
    cli_version: str | None = None
    contract_sha256: str | None = None
    #: Whether the runner has the external-token auth plane enabled. False on a
    #: runner that could otherwise start Codex but has no way to authenticate it.
    external_auth: bool | None = None

    def to_wire(self) -> dict[str, Any]:
        return {
            "engine": self.engine.value,
            "available": self.available,
            "reason": self.reason,
            "cliVersion": self.cli_version,
            "contractSha256": self.contract_sha256,
            "externalAuth": self.external_auth,
        }


#: What a daemon that predates ``/info.engines`` is taken to mean.
LEGACY_ENGINES: Final[dict[AgentEngine, EngineAvailability]] = {
    AgentEngine.CLAUDE_CODE: EngineAvailability(
        engine=AgentEngine.CLAUDE_CODE, available=True
    ),
}


def build_engines_info(codex_availability: Any | None) -> dict[str, dict[str, Any]]:
    """Compose the ``/info.engines`` payload for this daemon.

    ``codex_availability`` is a :class:`server.codex.capabilities.CodexAvailability`
    or ``None`` when the manager could not be built at all (disabled, or a build
    that raised — the daemon keeps serving Claude either way). Duck-typed rather
    than imported so this module stays importable on an edge with no Codex extras.

    Claude Code is unconditionally available: the daemon IS the Claude runtime, and
    a daemon that could not run Claude would not have started.
    """
    engines: dict[str, dict[str, Any]] = {
        AgentEngine.CLAUDE_CODE.value: EngineAvailability(
            engine=AgentEngine.CLAUDE_CODE, available=True
        ).to_wire(),
    }
    if codex_availability is None:
        engines[AgentEngine.CODEX.value] = EngineAvailability(
            engine=AgentEngine.CODEX,
            available=False,
            reason="disabled",
            external_auth=False,
        ).to_wire()
        return engines
    reason = getattr(codex_availability, "reason", None)
    engines[AgentEngine.CODEX.value] = EngineAvailability(
        engine=AgentEngine.CODEX,
        available=bool(getattr(codex_availability, "available", False)),
        reason=getattr(reason, "value", reason),
        cli_version=getattr(codex_availability, "cli_version", None),
        contract_sha256=getattr(codex_availability, "contract_sha256", None),
        external_auth=getattr(codex_availability, "external_auth", None),
    ).to_wire()
    return engines


def parse_engines_info(info: Mapping[str, Any] | None) -> dict[AgentEngine, EngineAvailability]:
    """Read ``/info`` (or a persisted snapshot of its ``engines`` key).

    Accepts either the whole ``/info`` dict or just the ``engines`` sub-dict, since
    the registry stores only the latter. An absent ``engines`` key falls back to
    :data:`LEGACY_ENGINES`; an unparseable entry is dropped rather than raising —
    a malformed advertisement means "not offered", never a 500 on the fleet page.
    """
    if not info:
        return dict(LEGACY_ENGINES)
    raw = info.get(ENGINES_INFO_KEY, _MISSING)
    if raw is _MISSING:
        # Either a legacy /info, or an already-unwrapped engines dict.
        raw = info if _looks_like_engines_map(info) else None
        if raw is None:
            return dict(LEGACY_ENGINES)
    if not isinstance(raw, Mapping):
        return dict(LEGACY_ENGINES)
    out: dict[AgentEngine, EngineAvailability] = {}
    for key, value in raw.items():
        try:
            engine = AgentEngine(key)
        except (TypeError, ValueError):
            continue
        if not isinstance(value, Mapping):
            continue
        out[engine] = EngineAvailability(
            engine=engine,
            available=bool(value.get("available")),
            reason=_as_str(value.get("reason")),
            cli_version=_as_str(value.get("cliVersion")),
            contract_sha256=_as_str(value.get("contractSha256")),
            external_auth=(
                bool(value["externalAuth"])
                if isinstance(value.get("externalAuth"), bool)
                else None
            ),
        )
    return out


def expected_contract_sha256(engine: AgentEngine | None) -> str | None:
    """The app-server schema contract THIS process is pinned to, for *engine*.

    Compared against what a runner advertised, so a runner upgraded past the edge is
    filtered out rather than handed a launch it would translate with a different
    vocabulary. ``None`` for engines with no such contract (Claude), and ``None``
    when the Codex module cannot be imported — an edge built without the extras must
    degrade to "no extra constraint", not raise on the dispatch path.
    """
    if engine is not AgentEngine.CODEX:
        return None
    try:
        from server.codex.schema import PINNED_CONTRACT_SHA256
    except Exception:  # pragma: no cover — an edge built without the codex extras
        return None
    return PINNED_CONTRACT_SHA256


def engine_dispatchable(
    engines: Mapping[AgentEngine, EngineAvailability],
    engine: AgentEngine,
    *,
    expected_contract_sha256: str | None = None,
) -> tuple[bool, str | None]:
    """May a launch for *engine* be sent to a runner advertising *engines*?

    Returns ``(ok, reason)``; ``reason`` is a short, stable, credential-free string
    suitable for an API response and a log line.

    Claude Code is never gated here — the compatibility default above already made
    every legacy runner Claude-capable, and re-deriving a second verdict for it
    would be the parallel implementation this module exists to prevent.
    """
    if engine is AgentEngine.CLAUDE_CODE:
        return True, None
    state = engines.get(engine)
    if state is None:
        return False, "engine_not_advertised"
    if not state.available:
        return False, state.reason or "unavailable"
    if (
        expected_contract_sha256
        and state.contract_sha256
        and state.contract_sha256 != expected_contract_sha256
    ):
        return False, "contract_hash_mismatch"
    return True, None


def engines_to_wire(
    engines: Mapping[AgentEngine, EngineAvailability],
) -> dict[str, dict[str, Any]]:
    """Round-trip helper: typed map back to the JSON shape stored/served."""
    return {engine.value: state.to_wire() for engine, state in engines.items()}


class _Missing:
    __slots__ = ()


_MISSING = _Missing()


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _looks_like_engines_map(value: Mapping[str, Any]) -> bool:
    """True when *value* is already the ``engines`` sub-dict rather than ``/info``.

    Deliberately strict: EVERY key must name a known engine. ``/info`` itself has
    keys like ``sha``/``protocol``, so it can never be mistaken for one.
    """
    if not value:
        return False
    for key in value:
        try:
            AgentEngine(key)
        except (TypeError, ValueError):
            return False
    return True
