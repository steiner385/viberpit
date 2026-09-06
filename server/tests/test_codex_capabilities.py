"""The Codex engine's capability set and its typed availability report."""
from __future__ import annotations

import dataclasses
import json

import pytest

from server.codex import schema
from server.codex.capabilities import (
    CODEX_CAPABILITIES,
    CODEX_ENGINE,
    NO_CAPABILITIES,
    CodexAvailability,
    CodexUnavailableReason,
)
from server.sessions import engine as engine_mod


def test_capabilities_use_the_canonical_dataclass():
    """One capability vocabulary for every engine — never a Codex-shaped copy."""
    assert isinstance(CODEX_CAPABILITIES, engine_mod.AgentCapabilities)
    assert CODEX_ENGINE is engine_mod.AgentEngine.CODEX


def test_capability_set_covers_every_declared_field():
    declared = {f.name for f in dataclasses.fields(engine_mod.AgentCapabilities)}
    assert declared == {
        "models", "reasoning_effort", "steer", "interrupt", "approvals",
        "questions", "fork", "adopt", "self_model_switch", "subagents", "attachments",
        "account_rotation", "account_switch_new_thread",
    }


def test_the_driver_binds_this_declaration_rather_than_restating_it():
    """One declaration per engine. A second literal on the driver class is how the
    availability report and the running driver drifted apart on ``questions``."""
    from server.codex.driver import CodexAgentDriver

    assert CodexAgentDriver.capabilities is CODEX_CAPABILITIES


def test_codex_advertises_the_planned_controls():
    assert CODEX_CAPABILITIES.models and CODEX_CAPABILITIES.reasoning_effort
    assert CODEX_CAPABILITIES.steer and CODEX_CAPABILITIES.interrupt
    assert CODEX_CAPABILITIES.approvals and CODEX_CAPABILITIES.attachments
    # Claude-CLI-private surfaces have no app-server equivalent in v1.
    assert not CODEX_CAPABILITIES.fork
    assert not CODEX_CAPABILITIES.adopt
    assert not CODEX_CAPABILITIES.self_model_switch
    assert not CODEX_CAPABILITIES.subagents
    # No interactive-question channel on the app-server, and rotation composes the
    # target account's CLAUDE config dir — a Codex session is refused by that path.
    assert not CODEX_CAPABILITIES.questions
    assert not CODEX_CAPABILITIES.account_rotation
    # ...but a fresh thread on the target account's OWN app-server needs none of
    # that Claude-specific machinery, so the honest fallback is available (#1275).
    assert CODEX_CAPABILITIES.account_switch_new_thread


def test_no_capabilities_is_all_false():
    assert NO_CAPABILITIES == engine_mod.AgentCapabilities()
    assert not any(
        getattr(NO_CAPABILITIES, f.name)
        for f in dataclasses.fields(engine_mod.AgentCapabilities)
    )


@pytest.mark.parametrize("reason", list(CodexUnavailableReason))
def test_an_unavailable_engine_advertises_nothing(reason):
    got = CodexAvailability.unavailable(reason, "because")
    assert got.capabilities == NO_CAPABILITIES
    assert got.reason is reason
    assert not got.available


def test_a_ready_engine_advertises_the_codex_set():
    got = CodexAvailability.ready(
        binary_path="/usr/bin/codex", cli_version=schema.PINNED_CLI_VERSION
    )
    assert got.capabilities == CODEX_CAPABILITIES
    assert got.reason is None


def test_available_and_reason_cannot_disagree():
    with pytest.raises(ValueError):
        CodexAvailability(available=True, reason=CodexUnavailableReason.DISABLED)
    with pytest.raises(ValueError):
        CodexAvailability(available=False, reason=None)


def test_detail_is_scrubbed_on_construction():
    got = CodexAvailability.unavailable(
        CodexUnavailableReason.VERSION_PROBE_FAILED,
        "exec failed with key sk-proj-abcdefghijklmnop",
    )
    assert "sk-proj-abcdefghijklmnop" not in got.detail
    assert "[REDACTED]" in got.detail


def test_wire_form_is_json_safe_and_names_the_engine():
    drift = schema.check_contract(cli_version="codex-cli 9.9.9", contract_sha256="nope")
    got = CodexAvailability.ready(
        binary_path="/usr/bin/codex", cli_version="codex-cli 9.9.9", accepted_drift=drift
    )
    wire = json.loads(json.dumps(got.to_wire()))
    assert wire["engine"] == "codex"
    assert wire["available"] is True
    assert wire["reason"] is None
    assert wire["capabilities"]["reasoningEffort"] is True
    assert wire["capabilities"]["selfModelSwitch"] is False
    assert set(wire["acceptedDrift"]) == {
        schema.ContractDriftReason.CLI_VERSION.value,
        schema.ContractDriftReason.CONTRACT_HASH.value,
    }


def test_unavailable_wire_form_carries_the_reason_code():
    wire = CodexAvailability.unavailable(
        CodexUnavailableReason.BINARY_NOT_FOUND, "'codex' is not installed"
    ).to_wire()
    assert wire["reason"] == "binary_not_found"
    assert wire["capabilities"]["models"] is False
