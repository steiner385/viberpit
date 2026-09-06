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
from server.sessions.engine import AgentCapabilities, AgentEngine, capabilities_to_wire

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
#: include images (``schema.DEFAULT_INPUT_MODALITIES``). ``CodexAgentDriver``
#: additionally folds text/CSV/JSON files and zip archives into an inline
#: `TextUserInput` (there is no native document modality on the wire); PDF has
#: neither and stays refused.
#: ``questions`` is True too (#1271): the app-server declares `item/tool/
#: requestUserInput` — Codex's own `AskUserQuestion` equivalent (header,
#: question, options, free text, per-question secrecy), marked EXPERIMENTAL by
#: the vendor schema the same way the external-token login surface is marked
#: UNSTABLE. It was found declared-but-unpinned when this gap was audited, not
#: absent — pinning its schema and answering it through the SAME
#: ``QuestionRegistry``/``ev.Question`` the Claude driver uses (never a parallel
#: question stack) is what closes the gap. `mcpServer/elicitation/request` is a
#: *different* surface (a third-party MCP tool server prompting the user) that
#: stays declared-but-unsupported: no MCP server is wired up for a Codex thread
#: yet, so no live session can ever raise it.
#:
#: ``subagents`` stays False too, but — like ``questions`` before #1271 closed
#: it — this was found declared-but-unwired, not absent (#1274). The app-server
#: has two real collaboration events: `subAgentActivity` (`agentPath`,
#: `agentThreadId`, a `kind` lifecycle of started/interacted/interrupted/
#: completed) and `collabAgentToolCall` (`tool` — spawnAgent/sendInput/wait/
#: interruptAgent/etc. — plus `senderThreadId`, `receiverThreadIds` and an
#: `agentsStates` map of each collaborator's own status/message). Both are
#: already normalized into ordinary `Task` `ToolCall`/`ToolResult` events
#: (`server/codex/translator.py`), which `server/sessions/subagents.py`
#: already recognizes by tool name — so a Codex session's collaboration
#: activity already renders as generic Task cards and would already populate
#: the dashboard's sub-agent summary once this flag is on. What is NOT yet
#: verified against a live binary, and so keeps this False per the "keep
#: unavailable capabilities visibly gated until verified" rule: whether a real
#: multi-call collab sequence (spawn → sendInput/wait → close) collapses into
#: one dashboard entry or fans out into several unrelated-looking ones (there
#: is no shared "agent id" in the wire payload to key on — `receiverThreadIds`/
#: `agentsStates` carry the real identity but their values are native thread
#: ids, which per `test_sub_agent_activity_never_carries_the_native_agent_thread_id`
#: must not ride in a generic wire payload), and there is no transcript-drill-down
#: for a child agent's own conversation (no fetch-by-thread-id surface exists
#: yet, though `CodexThreadReplay` over the already-wired `thread/read` RPC is
#: the natural mechanism once that is designed). Flipping this flag is
#: therefore the next step, not this one.
#:
#: The remaining ``False`` entries are equally deliberate: ccswitch's fork,
#: adopt and self-directed model switching surfaces are all implemented
#: against the Claude CLI's private state and have no app-server equivalent in
#: v1.
#: ``account_rotation`` is False because rotation composes the target account's
#: Claude config dir and syncs Claude credentials back; a Codex session is refused
#: by that path rather than rebuilt as some other engine's driver. That refusal is
#: architectural, not a gap to close later (#1275): Codex isolates every account
#: through its own `codex app-server` process and `CODEX_HOME`
#: (server/codex/process.py), so there is no in-process credential swap and no
#: app-server surface to resume a live thread under a different account's
#: identity — verified against the pinned app-server protocol
#: (server/codex/schema.py), not assumed.
#: ``account_switch_new_thread`` is True, though: nothing stops a fresh thread
#: from starting on the target account's own app-server, so a manual switch or a
#: usage-ceiling rotation still moves the session — honestly, as a new
#: conversation rather than a silently continued one.
CODEX_CAPABILITIES: Final[AgentCapabilities] = AgentCapabilities(
    models=True,
    reasoning_effort=True,
    steer=True,
    interrupt=True,
    approvals=True,
    questions=True,
    fork=False,
    adopt=False,
    self_model_switch=False,
    subagents=False,
    attachments=True,
    account_rotation=False,
    account_switch_new_thread=True,
    # The app-server publishes a catalog through `skills/list` and invokes one
    # of its entries through the `SkillUserInput` variant of `turn/start` — both
    # documented in the pinned contract, neither synthesized (#1286). It has no
    # built-in slash commands beyond that, so what the palette offers a Codex
    # session is: this catalog, plus the app-owned commands below.
    vendor_commands=True,
    vendor_skills=True,
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
    #: The app-server schema contract this runner actually observed. Carried so the
    #: EDGE can compare it against its own pin instead of trusting a runner's
    #: verdict — a runner upgraded past the edge is the mixed-version rollout the
    #: fleet gate exists to catch. ``None`` when the probe never got that far.
    contract_sha256: str | None = None
    #: Whether the runner's external-token auth plane is enabled. ``None`` when the
    #: question was not reached (the probe short-circuits on ``DISABLED``).
    external_auth: bool | None = None

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
        contract_sha256: str | None = None,
        external_auth: bool | None = None,
    ) -> CodexAvailability:
        return cls(
            available=False,
            reason=reason,
            detail=detail,
            binary_path=binary_path,
            cli_version=cli_version,
            contract_sha256=contract_sha256,
            external_auth=external_auth,
        )

    @classmethod
    def ready(
        cls,
        *,
        binary_path: str,
        cli_version: str,
        accepted_drift: schema.ContractDrift | None = None,
        contract_sha256: str | None = None,
    ) -> CodexAvailability:
        return cls(
            available=True,
            binary_path=binary_path,
            cli_version=cli_version,
            accepted_drift=accepted_drift,
            contract_sha256=contract_sha256,
            # Reaching `ready` means the external-auth gate was passed.
            external_auth=True,
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
            "capabilities": capabilities_to_wire(self.capabilities),
        }
