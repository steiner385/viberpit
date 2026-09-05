"""The single engine-advertisement parser and dispatch verdict.

Every fleet surface — the daemon's `/info`, the probe's persisted snapshot, the
edge's pre-dispatch gate, the fleet page, `/api/config` — reads engine
availability through this module. These tests pin the two rules that are easy to
get subtly wrong in one of those five places and nowhere else: what an ABSENT
advertisement means (legacy Claude-only, for exactly one upgrade window) and what
an EMPTY one means (a daemon that answered "nothing", which is not the same
thing).
"""
from __future__ import annotations

from ccswitch.engines import AgentEngine
from server.codex.capabilities import CodexAvailability, CodexUnavailableReason
from server.sessions.host_engines import (
    ENGINES_INFO_KEY,
    EngineAvailability,
    build_engines_info,
    engine_dispatchable,
    engines_to_wire,
    expected_contract_sha256,
    parse_engines_info,
)


def _codex(**kw) -> dict[AgentEngine, EngineAvailability]:
    return {
        AgentEngine.CLAUDE_CODE: EngineAvailability(
            engine=AgentEngine.CLAUDE_CODE, available=True),
        AgentEngine.CODEX: EngineAvailability(engine=AgentEngine.CODEX, **kw),
    }


# --- the compatibility default ------------------------------------------------

def test_info_without_engines_is_read_as_legacy_claude_only():
    """The rolling-upgrade window: an old daemon advertises nothing and means Claude."""
    engines = parse_engines_info({"sha": "abc", "protocol": 1})
    assert set(engines) == {AgentEngine.CLAUDE_CODE}
    assert engines[AgentEngine.CLAUDE_CODE].available is True


def test_missing_snapshot_is_also_legacy_claude_only():
    """A host row that has never been probed reads the same as a legacy daemon."""
    assert parse_engines_info(None) == parse_engines_info({})


def test_an_empty_advertisement_is_not_the_legacy_default():
    """`engines: {}` is a daemon SAYING it offers nothing, not one that stayed silent.

    Collapsing the two would make a daemon that deliberately reports no engines
    look Claude-capable — the exact opposite of what it said.
    """
    engines = parse_engines_info({ENGINES_INFO_KEY: {}})
    assert engines == {}
    ok, reason = engine_dispatchable(engines, AgentEngine.CLAUDE_CODE)
    assert ok is True and reason is None  # claude is never gated; see the module docstring


def test_an_already_unwrapped_engines_map_round_trips():
    """The registry persists only the sub-dict, so the parser must accept both shapes."""
    wire = build_engines_info(CodexAvailability.ready(
        binary_path="/usr/bin/codex", cli_version="codex-cli 0.153.2",
        contract_sha256="deadbeef"))
    assert parse_engines_info(wire) == parse_engines_info({ENGINES_INFO_KEY: wire})


def test_a_malformed_entry_is_dropped_rather_than_raising():
    """A hostile or merely broken runner must not 500 the fleet page."""
    engines = parse_engines_info({ENGINES_INFO_KEY: {
        "codex": "not-a-mapping",
        "not_an_engine": {"available": True},
        "claude_code": {"available": True},
    }})
    assert set(engines) == {AgentEngine.CLAUDE_CODE}


# --- the verdict --------------------------------------------------------------

def test_claude_is_dispatchable_even_on_a_runner_that_never_mentioned_it():
    ok, reason = engine_dispatchable({}, AgentEngine.CLAUDE_CODE)
    assert (ok, reason) == (True, None)


def test_an_unadvertised_codex_is_refused_with_a_nameable_reason():
    ok, reason = engine_dispatchable(
        parse_engines_info(None), AgentEngine.CODEX)
    assert ok is False
    assert reason == "engine_not_advertised"


def test_an_unavailable_codex_surfaces_the_runners_own_reason():
    ok, reason = engine_dispatchable(
        _codex(available=False, reason="binary_not_found"), AgentEngine.CODEX)
    assert (ok, reason) == (False, "binary_not_found")


def test_a_runner_on_a_different_contract_is_refused():
    """The mixed-version rollout: the runner is happy, the EDGE is not pinned there."""
    ok, reason = engine_dispatchable(
        _codex(available=True, contract_sha256="runner-hash"),
        AgentEngine.CODEX,
        expected_contract_sha256="edge-hash",
    )
    assert (ok, reason) == (False, "contract_hash_mismatch")


def test_a_matching_contract_is_dispatchable():
    ok, reason = engine_dispatchable(
        _codex(available=True, contract_sha256="same"),
        AgentEngine.CODEX,
        expected_contract_sha256="same",
    )
    assert (ok, reason) == (True, None)


def test_a_runner_that_reports_no_contract_is_not_penalised_for_it():
    """An older advertisement without the hash must not become an outage.

    The hash is additive; refusing on its absence would fence every runner that
    upgraded to engine advertisement but not to hash reporting.
    """
    ok, _ = engine_dispatchable(
        _codex(available=True), AgentEngine.CODEX, expected_contract_sha256="edge-hash")
    assert ok is True


def test_expected_contract_is_only_asked_of_codex():
    from server.codex.schema import PINNED_CONTRACT_SHA256

    assert expected_contract_sha256(AgentEngine.CLAUDE_CODE) is None
    assert expected_contract_sha256(None) is None
    assert expected_contract_sha256(AgentEngine.CODEX) == PINNED_CONTRACT_SHA256


# --- what the daemon publishes ------------------------------------------------

def test_a_daemon_with_no_codex_manager_still_advertises_claude():
    """Codex failure is isolated: a daemon that cannot build the manager serves Claude."""
    wire = build_engines_info(None)
    assert wire["claude_code"]["available"] is True
    assert wire["codex"]["available"] is False
    assert wire["codex"]["reason"] == "disabled"


def test_a_ready_codex_publishes_the_version_and_contract_it_verified():
    wire = build_engines_info(CodexAvailability.ready(
        binary_path="/usr/bin/codex",
        cli_version="codex-cli 0.153.2",
        contract_sha256="abc123",
    ))
    assert wire["codex"] == {
        "engine": "codex",
        "available": True,
        "reason": None,
        "cliVersion": "codex-cli 0.153.2",
        "contractSha256": "abc123",
        "externalAuth": True,
    }


def test_an_unavailable_codex_publishes_its_typed_reason():
    wire = build_engines_info(CodexAvailability.unavailable(
        CodexUnavailableReason.BINARY_NOT_FOUND, "'codex' is not installed"))
    assert wire["codex"]["available"] is False
    assert wire["codex"]["reason"] == "binary_not_found"


def test_the_advertisement_carries_no_free_text_at_all():
    """`CodexAvailability.detail` is scrubbed to a fixed literal by design.

    Publishing it would put "[REDACTED]" in front of an operator instead of a
    cause, and would put an unbounded, peer-influenced string into a payload polled
    once per host every 30 seconds. The typed reason IS the contract.
    """
    wire = build_engines_info(CodexAvailability.unavailable(
        CodexUnavailableReason.VERSION_PROBE_FAILED, "sk-proj-" + "x" * 5000))
    assert "detail" not in wire["codex"]
    assert set(wire["codex"]) == {
        "engine", "available", "reason", "cliVersion", "contractSha256", "externalAuth"}


def test_wire_round_trips_through_the_parser():
    wire = build_engines_info(CodexAvailability.ready(
        binary_path="/usr/bin/codex", cli_version="v", contract_sha256="h"))
    assert engines_to_wire(parse_engines_info(wire)) == wire
