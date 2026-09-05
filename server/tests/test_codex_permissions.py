"""ccswitch permission mode -> Codex sandbox/approval profile (plan Phase 4).

The mapping in ``server.codex.permissions`` is a *security* boundary: it decides
how much authority a Codex thread gets for a mode the operator chose while
thinking about Claude. So most of what follows deliberately does NOT restate the
table — it derives the constraint from the real permission engine
(``server.sessions.permissions.PermissionRegistry`` / ``to_sdk_permission_mode``)
and asserts the mapping cannot exceed it. Rewriting the table in a test would
only prove the table equals itself.
"""
import asyncio

import pytest

from server.codex.permissions import (
    PROFILES,
    CodexApprovalPolicy,
    CodexPermissionProfile,
    CodexSandboxMode,
    UnsupportedCodexPermissionMode,
    profile_for_mode,
    supported_modes,
)
from server.sessions.permissions import PermissionRegistry, to_sdk_permission_mode
from server.sessions.types import VALID_SESSION_MODES

# Probe tools spanning the classes PermissionRegistry distinguishes: read-only,
# the "safe but not read-only" middle (TodoWrite), file writes, shell, network.
PROBE_TOOLS = ("Read", "Grep", "TodoWrite", "Edit", "Write", "Bash", "WebFetch")

# Natural permissiveness order of the two app-server enums (how much authority
# each value grants), NOT a copy of the mapping under test.
_SANDBOX_RANK = {
    CodexSandboxMode.READ_ONLY: 0,
    CodexSandboxMode.WORKSPACE_WRITE: 1,
    CodexSandboxMode.DANGER_FULL_ACCESS: 2,
}
_APPROVAL_RANK = {
    CodexApprovalPolicy.UNTRUSTED: 0,
    CodexApprovalPolicy.ON_REQUEST: 1,
    CodexApprovalPolicy.NEVER: 2,
}


def _auto_approved(mode: str) -> frozenset[str]:
    """The probe tools ccswitch runs with NO human prompt in *mode*.

    Asks the real registry rather than reading its source, so this tracks any
    future change to what a mode auto-approves.
    """

    async def run() -> frozenset[str]:
        reg = PermissionRegistry()
        allowed = set()
        for tool in PROBE_TOOLS:
            perm_id, fut, _always = await reg.request(
                "s1", "t1", tool, {"command": "echo hi", "file_path": "/tmp/x"},
                mode=mode, denylist=PermissionRegistry.DANGEROUS,
                allowlist=True, identity="tester")
            if perm_id is None:
                assert fut.result() is True
                allowed.add(tool)
        reg.deny_all("s1")
        return frozenset(allowed)

    return asyncio.run(run())


# ---------------------------------------------------------------------------
# Vocabulary: the enums must be the app-server's, and the table must cover the
# modes ccswitch can actually hand a driver.
# ---------------------------------------------------------------------------

def test_enums_are_the_app_server_vocabulary():
    # v2/ThreadStartParams.json: SandboxMode enum, AskForApproval's string arm.
    # The object-shaped GranularAskForApproval variant is out of scope.
    assert {m.value for m in CodexSandboxMode} == {
        "read-only", "workspace-write", "danger-full-access"}
    assert {p.value for p in CodexApprovalPolicy} == {
        "untrusted", "on-request", "never"}


def test_every_ccswitch_session_mode_is_mapped():
    missing = VALID_SESSION_MODES - set(PROFILES)
    assert not missing, f"unmapped ccswitch session modes: {sorted(missing)}"


def test_transient_driver_modes_are_mapped():
    # ClaudeAgentDriver.set_permission_mode receives these two straight from the
    # web plan toggle; they are not in VALID_SESSION_MODES but do reach a driver.
    for mode in ("plan", "default"):
        assert mode in PROFILES


def test_supported_modes_agrees_with_profiles():
    assert set(supported_modes()) == set(PROFILES)
    assert supported_modes() == tuple(sorted(supported_modes())), "stable order"
    for mode in supported_modes():
        assert profile_for_mode(mode) is PROFILES[mode]


def test_profiles_are_frozen_and_slotted():
    profile = profile_for_mode("interactive")
    assert isinstance(profile, CodexPermissionProfile)
    with pytest.raises(Exception):
        profile.sandbox_mode = CodexSandboxMode.DANGER_FULL_ACCESS  # type: ignore[misc]
    assert not hasattr(profile, "__dict__"), "slots=True keeps the DTO cheap+closed"


# ---------------------------------------------------------------------------
# The mapping itself (the one place the table is spelled out).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("mode", "sandbox", "approval"), [
    ("plan", CodexSandboxMode.READ_ONLY, CodexApprovalPolicy.UNTRUSTED),
    ("interactive", CodexSandboxMode.WORKSPACE_WRITE, CodexApprovalPolicy.UNTRUSTED),
    ("default", CodexSandboxMode.WORKSPACE_WRITE, CodexApprovalPolicy.UNTRUSTED),
    ("allowlist", CodexSandboxMode.WORKSPACE_WRITE, CodexApprovalPolicy.UNTRUSTED),
    ("auto", CodexSandboxMode.WORKSPACE_WRITE, CodexApprovalPolicy.UNTRUSTED),
    ("acceptEdits", CodexSandboxMode.WORKSPACE_WRITE, CodexApprovalPolicy.UNTRUSTED),
    ("full-auto", CodexSandboxMode.WORKSPACE_WRITE, CodexApprovalPolicy.NEVER),
    ("bypassPermissions", CodexSandboxMode.DANGER_FULL_ACCESS, CodexApprovalPolicy.NEVER),
])
def test_mode_maps_to_expected_profile(mode, sandbox, approval):
    profile = profile_for_mode(mode)
    assert profile.sandbox_mode is sandbox
    assert profile.approval_policy is approval


def test_unknown_mode_raises_instead_of_downgrading():
    with pytest.raises(UnsupportedCodexPermissionMode) as excinfo:
        profile_for_mode("yolo")
    assert excinfo.value.mode == "yolo"
    assert "yolo" in str(excinfo.value)
    # The error names what IS supported, so the caller never has to guess.
    assert "interactive" in str(excinfo.value)
    assert isinstance(excinfo.value, ValueError)


def test_unknown_mode_error_is_raised_for_empty_and_cased_variants():
    for bad in ("", "Interactive", "BYPASSPERMISSIONS"):
        with pytest.raises(UnsupportedCodexPermissionMode):
            profile_for_mode(bad)


# ---------------------------------------------------------------------------
# The invariants (derived from the real permission engine, not from the table).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", sorted(PROFILES))
def test_a_mode_that_prompts_a_human_never_gets_an_unattended_profile(mode):
    """Rule (a): the Codex profile may not be MORE permissive than the ccswitch mode.

    If ccswitch would stop and ask a human for ANY probe tool in this mode, the
    Codex thread must keep an approval channel (``approval_policy != never``) and
    must not run outside a sandbox — otherwise switching engine silently removes
    the human from the loop the operator asked for.
    """
    prompts_for = set(PROBE_TOOLS) - _auto_approved(mode)
    if not prompts_for:
        pytest.skip(f"{mode} auto-approves every probe tool; nothing to constrain")
    profile = profile_for_mode(mode)
    assert profile.approval_policy is not CodexApprovalPolicy.NEVER, (
        f"ccswitch prompts for {sorted(prompts_for)} in {mode!r}, "
        "so Codex must still be able to ask")
    assert profile.sandbox_mode is not CodexSandboxMode.DANGER_FULL_ACCESS


@pytest.mark.parametrize("mode", sorted(PROFILES))
def test_only_a_permission_bypassing_mode_gets_the_unsandboxed_profile(mode):
    """``danger-full-access`` removes the sandbox entirely, so it is reserved for
    the one mode ccswitch itself defines as bypassing the engine's permission
    system (``to_sdk_permission_mode`` returns ``bypassPermissions`` only there)."""
    if profile_for_mode(mode).sandbox_mode is CodexSandboxMode.DANGER_FULL_ACCESS:
        assert to_sdk_permission_mode(mode) == "bypassPermissions"


def test_plan_mode_never_gets_a_write_capable_sandbox():
    """Rule (b): plan means 'propose, change nothing'."""
    profile = profile_for_mode("plan")
    assert profile.sandbox_mode is CodexSandboxMode.READ_ONLY
    assert not profile.sandbox_mode.write_capable
    assert profile.approval_policy is not CodexApprovalPolicy.NEVER


def test_plan_is_the_least_permissive_profile():
    plan = profile_for_mode("plan")
    for mode in supported_modes():
        other = profile_for_mode(mode)
        assert _SANDBOX_RANK[plan.sandbox_mode] <= _SANDBOX_RANK[other.sandbox_mode]
        assert _APPROVAL_RANK[plan.approval_policy] <= _APPROVAL_RANK[other.approval_policy]


def test_profile_permissiveness_is_monotone_in_ccswitch_permissiveness():
    """A strictly stricter ccswitch mode may never get a laxer Codex profile.

    "Stricter" is measured against the registry: mode A is stricter than B when
    the set of tools A auto-approves is a proper subset of B's.
    """
    approved = {mode: _auto_approved(mode) for mode in supported_modes()}
    for stricter, strict_set in approved.items():
        for laxer, lax_set in approved.items():
            if not (strict_set < lax_set):
                continue
            a, b = profile_for_mode(stricter), profile_for_mode(laxer)
            assert _SANDBOX_RANK[a.sandbox_mode] <= _SANDBOX_RANK[b.sandbox_mode], (
                f"{stricter} is stricter than {laxer} in ccswitch but gets a "
                "wider Codex sandbox")
            assert _APPROVAL_RANK[a.approval_policy] <= _APPROVAL_RANK[b.approval_policy], (
                f"{stricter} is stricter than {laxer} in ccswitch but asks for "
                "fewer Codex approvals")


@pytest.mark.parametrize("mode", sorted(PROFILES))
def test_description_is_operator_prose_that_claims_no_parity(mode):
    text = profile_for_mode(mode).description
    assert text and text[0].isupper() and len(text) > 20
    lowered = text.lower()
    for claim in ("same as", "equivalent to", "identical to", "parity"):
        assert claim not in lowered, f"{mode} description claims Claude parity"
