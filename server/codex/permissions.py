"""ccswitch permission mode -> Codex sandbox + approval policy.

This is the ONE place that answers "the operator picked mode *M*; how much
authority does a Codex thread get?". `thread/start`, `turn/start` and
`turn/settings/update` all read their `sandboxMode` / `askForApproval` from
here, so the launch path and a live mode switch cannot drift apart — the failure
`server.sessions.permissions.to_sdk_permission_mode` exists to prevent, in the
engine that has no SDK to fall back on.

The mode vocabulary is NOT restated here. It is
:data:`server.sessions.types.VALID_SESSION_MODES` (the six modes the API,
Settings and the SDLC tier resolver accept) plus the two transient values a
driver receives directly from the web plan toggle — see
:data:`TRANSIENT_DRIVER_MODES`.

Why the mapping looks the way it does
-------------------------------------
Codex and Claude gate different things, so a "translation" is a policy choice,
not an equivalence. Two axes exist on the Codex side (v2 `ThreadStartParams`):

* **sandbox** — `read-only` (no writes at all) < `workspace-write` (writes
  confined to the workspace, network off) < `danger-full-access` (no sandbox).
  It bounds what an action can touch *once it runs*.
* **approval** — `untrusted` (everything outside Codex's built-in trusted
  read-command set asks a human) < `on-request` (the model runs sandboxed work
  unattended and asks only to escalate) < `never` (nothing ever asks).
  It decides *whether a human is in the loop*.

ccswitch's axis is a third thing entirely: `PermissionRegistry.request` gates
per **tool name** (`Bash`, `Edit`, `WebFetch`, …). Codex has no tool-scoped
tier, so the three middle modes — `allowlist` ("read-only auto, prompt the
rest"), `auto` ("auto-approve safe tools") and `acceptEdits` ("accept file
edits, prompt the rest") — cannot be expressed. Each of them still prompts for
shell, so the only faithful-or-stricter choice is `untrusted`; picking
`on-request` to "match the vibe" would let Codex run arbitrary commands with no
human at all, which is strictly MORE authority than the operator granted. They
therefore collapse onto the interactive profile, and each `description` says so
rather than implying the Claude behaviour was reproduced. Erring strict is a
prompt the operator can answer; erring lax is an action they cannot take back.

Two rules are absolute:

1. **Never more permissive than the ccswitch mode.** If ccswitch would stop and
   ask a human for any tool in this mode, the profile keeps an approval channel
   (`approval_policy != never`) and keeps a sandbox. `danger-full-access` is
   reserved for `bypassPermissions`, the single mode ccswitch itself defines as
   bypassing the engine's permission system.
2. **`plan` never gets a write-capable sandbox.** Plan mode means "propose, do
   not change anything", so it maps to `read-only` — a guarantee enforced by the
   sandbox, not by asking the model to behave.

`full-auto` keeps `workspace-write` rather than `danger-full-access`: it is
already "approve everything without asking" on the ccswitch side (unattended
automations depend on that, so `never` is required for the approval axis), but
the workspace sandbox still bounds the blast radius at no cost to the operator's
intent. An operator who genuinely wants no guardrails has `bypassPermissions`.

`CodexApprovalPolicy.ON_REQUEST` is defined because it is part of the wire enum
(it is Codex's own default and must round-trip through parsers), but no profile
selects it: there is no ccswitch mode for which "run shell unattended, ask only
to leave the sandbox" is not an escalation of authority.

An unmapped mode raises :class:`UnsupportedCodexPermissionMode` rather than
falling back to a default. A silent fallback is how a mode ends up running with
more authority than its name promises; failing closed keeps the damage to "this
Codex session did not start", which degrades only Codex.

The object-shaped `GranularAskForApproval` variant of `AskForApproval` is out of
scope: ccswitch has no vocabulary for per-capability grants (the same reason
`item/permissions/requestApproval` stays unsupported).
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

#: Permission-mode values that reach a driver's ``set_permission_mode`` without
#: being members of ``VALID_SESSION_MODES``: the web plan toggle turns plan mode
#: ON with ``"plan"`` and OFF with the ``"default"`` sentinel (see
#: ``ClaudeAgentDriver.set_permission_mode``). Mapped here so the Codex driver
#: can honour the same toggle instead of failing on a value the UI always sends.
TRANSIENT_DRIVER_MODES: Final[tuple[str, ...]] = ("plan", "default")


class CodexSandboxMode(StrEnum):
    """`SandboxMode` from the app-server schema (v2 `ThreadStartParams.json`)."""

    READ_ONLY = "read-only"
    WORKSPACE_WRITE = "workspace-write"
    DANGER_FULL_ACCESS = "danger-full-access"

    @property
    def write_capable(self) -> bool:
        """True when the sandbox permits writing to the filesystem at all."""
        return self is not CodexSandboxMode.READ_ONLY

    @property
    def network_egress(self) -> bool:
        """True when a command run under this sandbox can reach the network.

        Only the unsandboxed mode can. Both `read-only` and `workspace-write`
        are network-isolated, and ccswitch cannot lift that: the pinned
        `ThreadStartParams` takes a bare `SandboxMode` string with no
        `sandboxPolicy` object, and the policy the app-server reports back
        defaults `workspaceWrite.networkAccess` to `false`. The knob exists in
        Codex's own config file, but managing Codex config is explicitly post-v1
        (see the plan's Definition of done), so for ccswitch this is a fact about
        the mode, not a setting.

        Asserted against the pinned fixture in
        ``server/tests/test_codex_sandbox_network_egress.py`` so a schema bump
        that adds a settable knob fails loudly instead of leaving this stale.
        """
        return self is CodexSandboxMode.DANGER_FULL_ACCESS


class CodexApprovalPolicy(StrEnum):
    """The plain-string arm of `AskForApproval` (v2 `ThreadStartParams.json`).

    The `{"granular": {...}}` object arm is deliberately not modelled.
    """

    UNTRUSTED = "untrusted"
    ON_REQUEST = "on-request"
    NEVER = "never"

    @property
    def prompts_human(self) -> bool:
        """True when a human can still be asked before an action runs."""
        return self is not CodexApprovalPolicy.NEVER


@dataclass(frozen=True, slots=True)
class CodexPermissionProfile:
    """What a ccswitch permission mode means to a Codex thread.

    ``description`` is operator-facing prose rendered next to the mode. It
    states what the **Codex** profile does — never that it reproduces the Claude
    mode, which it generally does not.
    """

    sandbox_mode: CodexSandboxMode
    approval_policy: CodexApprovalPolicy
    description: str

    @property
    def network_egress(self) -> bool:
        """True when work in this profile can reach the network.

        Derived from the sandbox rather than stored, so a profile cannot claim
        an egress its own sandbox forbids.
        """
        return self.sandbox_mode.network_egress


_PLAN = CodexPermissionProfile(
    sandbox_mode=CodexSandboxMode.READ_ONLY,
    approval_policy=CodexApprovalPolicy.UNTRUSTED,
    description=(
        "Codex runs in a read-only sandbox: it can read the workspace and plan, "
        "but no file write or patch can be applied, and anything beyond a trusted "
        "read command asks you first."
    ),
)

_ASK_EVERY_ACTION = CodexPermissionProfile(
    sandbox_mode=CodexSandboxMode.WORKSPACE_WRITE,
    approval_policy=CodexApprovalPolicy.UNTRUSTED,
    description=(
        "Codex asks before every command and file change except its own trusted "
        "read commands, and approved work stays inside the workspace with network "
        "access off. Codex approvals are per command, not per tool, so a mode that "
        "auto-approves some tools in Claude still prompts here."
    ),
)

_FULL_AUTO = CodexPermissionProfile(
    sandbox_mode=CodexSandboxMode.WORKSPACE_WRITE,
    approval_policy=CodexApprovalPolicy.NEVER,
    description=(
        "Codex runs commands and applies patches without asking, but stays inside "
        "the workspace sandbox with network access off; work that needs to escape "
        "the sandbox fails instead of prompting."
    ),
)

_BYPASS = CodexPermissionProfile(
    sandbox_mode=CodexSandboxMode.DANGER_FULL_ACCESS,
    approval_policy=CodexApprovalPolicy.NEVER,
    description=(
        "Codex runs unsandboxed with no approvals: full filesystem and network "
        "access, nothing is asked and nothing is confined. Use only where the host "
        "itself is the boundary."
    ),
)

#: The mapping. Read-only so a caller cannot mutate the security policy in place.
PROFILES: Final[Mapping[str, CodexPermissionProfile]] = MappingProxyType({
    # Transient driver-level overlay (web plan toggle).
    "plan": _PLAN,
    # "default" is the plan toggle's OFF sentinel; when a driver cannot restore a
    # remembered pre-plan mode it means "the engine's own gating", which for Codex
    # is the ask-before-acting profile — never a widening.
    "default": _ASK_EVERY_ACTION,
    # VALID_SESSION_MODES.
    "interactive": _ASK_EVERY_ACTION,
    "allowlist": _ASK_EVERY_ACTION,
    "auto": _ASK_EVERY_ACTION,
    "acceptEdits": _ASK_EVERY_ACTION,
    "full-auto": _FULL_AUTO,
    "bypassPermissions": _BYPASS,
})


class UnsupportedCodexPermissionMode(ValueError):
    """A permission mode Codex cannot express without granting more authority.

    Raised instead of returning a default profile: a Codex session that refuses
    to start is recoverable, a session running with authority nobody granted is
    not.
    """

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.supported: tuple[str, ...] = supported_modes()
        super().__init__(
            f"permission mode {mode!r} has no Codex sandbox/approval profile; "
            f"supported: {', '.join(self.supported)}"
        )


def supported_modes() -> tuple[str, ...]:
    """Every permission mode with a Codex profile, sorted for stable rendering.

    Callers (engine capability probes, the New Session dialog's Codex branch)
    enumerate this instead of hard-coding a list that would drift from
    :data:`PROFILES`.
    """
    return tuple(sorted(PROFILES))


def profile_for_mode(mode: str) -> CodexPermissionProfile:
    """The Codex profile for a ccswitch permission mode.

    Raises :class:`UnsupportedCodexPermissionMode` for anything unmapped — the
    mapping never falls back, and never downgrades to something more permissive.
    """
    try:
        return PROFILES[mode]
    except KeyError:
        raise UnsupportedCodexPermissionMode(mode) from None


def permits_network_egress(mode: str) -> bool:
    """Whether a Codex thread in ccswitch permission *mode* can reach the network.

    The question an UNATTENDED caller has to ask before it spends a session: an
    automation's own seed prompt ends in ``git push`` and ``gh pr create``, and
    under a network-isolated sandbox those fail after the whole run. Every mode
    except ``bypassPermissions`` answers False — approving every command without
    asking (``full-auto``) is an *approval* grant, not an egress one.

    Raises :class:`UnsupportedCodexPermissionMode` for an unmapped mode, like
    :func:`profile_for_mode`: a security question never gets a permissive
    default.
    """
    return profile_for_mode(mode).network_egress
