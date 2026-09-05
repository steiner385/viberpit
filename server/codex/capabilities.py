"""What the Codex engine can do, and why it might be able to do nothing.

Two things live here, because they are two halves of one question the rest of the
application asks — "may I offer Codex, and with which controls?":

* :data:`CODEX_CAPABILITIES`, the Codex engine's entry in the *existing*
  :class:`~server.sessions.engine.AgentCapabilities` vocabulary. The plan's "Fixed
  contracts" section is explicit that engine capabilities, not ``if engine == ...``
  branches, decide which controls render, so there is exactly one such value per
  engine and this is Codex's.
* :class:`CodexAvailability`, the typed answer to "is the Codex boundary usable on
  this runner right now?" — carrying a machine-readable
  :class:`CodexUnavailableReason` rather than an exception, because unavailability
  must degrade *only* Codex. A daemon that is also serving Claude sessions may not
  be crashed by a missing binary or a moved app-server contract.

An unavailable engine advertises **no** capabilities: ``availability.capabilities``
collapses to the all-false dataclass, so a UI that renders from capabilities cannot
offer a control backed by a process that will never start.

This module is pure types plus that one derivation. The probing that produces a
:class:`CodexAvailability` lives in :mod:`server.codex.process` (it needs the
filesystem and a subprocess); this module deliberately imports neither, so the
availability DTO stays importable from the edge, the daemon, and tests alike.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from server.codex import redaction, schema
from server.sessions.engine import AgentCapabilities, AgentEngine

CODEX_ENGINE: Final = AgentEngine.CODEX

#: The Codex engine's control surface — THE declaration, consumed both by the
#: availability report below and by ``CodexAgentDriver.capabilities`` (which binds
#: this value rather than restating it, so the "may I offer Codex?" answer and the
#: running driver can never disagree; they briefly did, on ``questions``).
#:
#: Every ``True`` here is a commitment made by the accepted plan, not a guess:
#: model and reasoning effort are named as the only Codex launch controls beside
#: the permission profile (spec D-series), and Phase 4's exit criterion is
#: "create/send/steer/interrupt/approve/replay/recover works through normalized
#: events" — which is exactly ``steer``, ``interrupt`` and ``approvals``.
#: ``attachments`` follows the app-server model contract, whose input modalities
#: include images (``schema.DEFAULT_INPUT_MODALITIES``).
#:
#: The ``False`` entries are equally deliberate: ccswitch's fork, adopt,
#: self-directed model switching and subagent surfaces are all implemented against
#: the Claude CLI's private state and have no app-server equivalent in v1.
#: ``questions`` joins them — the app-server has no interactive-question channel, so
#: ``CodexAgentDriver.answer_question`` can only ever report "no longer pending".
#: ``account_rotation`` is False because rotation composes the target account's
#: Claude config dir and syncs Claude credentials back; a Codex session is refused
#: by that path rather than rebuilt as some other engine's driver.
CODEX_CAPABILITIES: Final[AgentCapabilities] = AgentCapabilities(
    models=True,
    reasoning_effort=True,
    steer=True,
    interrupt=True,
    approvals=True,
    questions=False,
    fork=False,
    adopt=False,
    self_model_switch=False,
    subagents=False,
    attachments=True,
    account_rotation=False,
)

#: What an unavailable engine advertises. Not "unknown" — *nothing*.
NO_CAPABILITIES: Final[AgentCapabilities] = AgentCapabilities()


class CodexUnavailableReason(StrEnum):
    """Why Codex is not offered. Stable, machine-readable, safe to log or render."""

    DISABLED = "disabled"
    EXTERNAL_AUTH_DISABLED = "external_auth_disabled"
    BINARY_NOT_FOUND = "binary_not_found"
    VERSION_PROBE_FAILED = "version_probe_failed"
    UNSUPPORTED_VERSION = "unsupported_version"
    RUNTIME_HOME_REFUSED = "runtime_home_refused"


@dataclass(frozen=True, slots=True)
class CodexAvailability:
    """A runner's verdict on the Codex boundary.

    ``detail`` is free text for humans and is scrubbed on construction, because it
    quotes a binary path, a version string, or an OS error that a hostile or merely
    unlucky environment could have salted with something credential-shaped.
    """

    available: bool
    reason: CodexUnavailableReason | None = None
    detail: str = ""
    binary_path: str | None = None
    cli_version: str | None = None
    #: Set when the operator explicitly widened ``CCSWITCH_CODEX_SUPPORTED_VERSIONS``
    #: to admit a version that does not match the pin. Availability is granted, but
    #: the drift is carried so it can be logged and surfaced rather than forgotten.
    accepted_drift: schema.ContractDrift | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "detail", redaction.scrub_text(self.detail))
        if self.available and self.reason is not None:
            raise ValueError("an available engine cannot carry an unavailable reason")
        if not self.available and self.reason is None:
            raise ValueError("an unavailable engine must carry a typed reason")

    @classmethod
    def unavailable(
        cls,
        reason: CodexUnavailableReason,
        detail: str = "",
        *,
        binary_path: str | None = None,
        cli_version: str | None = None,
    ) -> CodexAvailability:
        return cls(
            available=False,
            reason=reason,
            detail=detail,
            binary_path=binary_path,
            cli_version=cli_version,
        )

    @classmethod
    def ready(
        cls,
        *,
        binary_path: str,
        cli_version: str,
        accepted_drift: schema.ContractDrift | None = None,
    ) -> CodexAvailability:
        return cls(
            available=True,
            binary_path=binary_path,
            cli_version=cli_version,
            accepted_drift=accepted_drift,
        )

    @property
    def capabilities(self) -> AgentCapabilities:
        """Codex's capability set, or nothing at all when it cannot run."""
        return CODEX_CAPABILITIES if self.available else NO_CAPABILITIES

    def to_wire(self) -> dict[str, Any]:
        """A JSON-safe summary for `/api/config`-style reporting."""
        return {
            "engine": CODEX_ENGINE.value,
            "available": self.available,
            "reason": self.reason.value if self.reason is not None else None,
            "detail": self.detail,
            "cliVersion": self.cli_version,
            "acceptedDrift": (
                list(self.accepted_drift.reason_codes())
                if self.accepted_drift is not None
                else None
            ),
            "capabilities": {
                "models": self.capabilities.models,
                "reasoningEffort": self.capabilities.reasoning_effort,
                "steer": self.capabilities.steer,
                "interrupt": self.capabilities.interrupt,
                "approvals": self.capabilities.approvals,
                "questions": self.capabilities.questions,
                "fork": self.capabilities.fork,
                "adopt": self.capabilities.adopt,
                "selfModelSwitch": self.capabilities.self_model_switch,
                "subagents": self.capabilities.subagents,
                "attachments": self.capabilities.attachments,
                "accountRotation": self.capabilities.account_rotation,
            },
        }
