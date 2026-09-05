"""Whether a Codex permission profile can reach the network at all.

Phase 7 Tasks 7.2/7.3 audit the unattended origins, and the audit turns on one
fact about the vendor contract: ``thread/start`` accepts a *plain*
``SandboxMode`` string and nothing else, while the resolved
``WorkspaceWriteSandboxPolicy`` the app-server reports back defaults
``networkAccess`` to ``false``. So ``workspace-write`` means "no network", and
ccswitch has no wire knob to say otherwise.

That is a claim about a pinned third-party schema, so it is asserted against the
pinned fixture rather than restated in prose (constitution III: verify against
the real contract). If a future pin adds a settable ``sandboxPolicy``, the
evidence test fails and whoever bumps the pin gets to revisit the mapping
instead of inheriting a stale assumption.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from server.codex.permissions import (
    CodexSandboxMode,
    permits_network_egress,
    profile_for_mode,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "codex_app_server" / "0.153.2"


def test_thread_start_offers_no_settable_network_knob():
    """The evidence for the whole mapping: the only sandbox input is the enum."""
    params = json.loads((_FIXTURES / "v2__ThreadStartParams.json").read_text())
    props = params["properties"]
    assert "sandboxPolicy" not in props, (
        "the pin now lets a caller send a SandboxPolicy object; "
        "re-derive permits_network_egress instead of keeping this mapping"
    )
    # `sandbox` is the bare enum (wrapped in an anyOf with null), so there is
    # nowhere to attach networkAccess on the way in.
    assert params["definitions"]["SandboxMode"]["enum"] == [
        "read-only", "workspace-write", "danger-full-access"]


def test_workspace_write_defaults_network_access_off_in_the_pinned_schema():
    """The other half: even the reported policy defaults the network off."""
    response = json.loads((_FIXTURES / "v2__ThreadStartResponse.json").read_text())
    workspace_write = next(
        arm for arm in response["definitions"]["SandboxPolicy"]["oneOf"]
        if arm["title"] == "WorkspaceWriteSandboxPolicy")
    assert workspace_write["properties"]["networkAccess"]["default"] is False


@pytest.mark.parametrize("sandbox,expected", [
    (CodexSandboxMode.READ_ONLY, False),
    (CodexSandboxMode.WORKSPACE_WRITE, False),
    (CodexSandboxMode.DANGER_FULL_ACCESS, True),
])
def test_only_an_unsandboxed_thread_reaches_the_network(sandbox, expected):
    assert sandbox.network_egress is expected


@pytest.mark.parametrize("mode", ["plan", "interactive", "allowlist", "auto",
                                  "acceptEdits", "full-auto", "default"])
def test_every_sandboxed_mode_is_network_isolated(mode):
    """Including ``full-auto`` — the automation default. This is the finding that
    blocks unattended Codex parity: approving every command without asking does
    NOT grant the egress a `git push` needs."""
    assert permits_network_egress(mode) is False
    assert profile_for_mode(mode).network_egress is False


def test_bypass_permissions_is_the_only_mode_that_reaches_the_network():
    assert permits_network_egress("bypassPermissions") is True


def test_an_unmapped_mode_fails_closed_rather_than_claiming_egress():
    """Consistent with ``profile_for_mode``: never answer a security question
    permissively for a mode nobody mapped."""
    from server.codex.permissions import UnsupportedCodexPermissionMode

    with pytest.raises(UnsupportedCodexPermissionMode):
        permits_network_egress("nonesuch")
