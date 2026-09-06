"""Provider-neutral automation launch contract (Phase 7, Task 7.1).

These are PURE tests of ``server/automations/engine_policy.py`` plus the audited
policy map. The security property under test is one sentence: **an origin that
has not passed a Codex acceptance suite cannot spawn a Codex session**, no
matter what a config row, an API caller, or a hand-edited database says.
"""
from __future__ import annotations

import pytest

from ccswitch.engines import AgentEngine
from server.automations.engine_policy import (
    ENGINE_CONFIG_KEY,
    EnginePolicy,
    EnginePolicyViolation,
    allowed_engines,
    audited_policy_map,
    default_engine,
    effective_config_schema,
    engine_config_fields,
    parse_engine_policy,
    policy_for,
    resolve_engine,
    sanitize_launch_for_engine,
)
from server.automations.registry import registry


# --------------------------------------------------------------------------- #
# The audited map. This test is the reviewable record of which origins may run
# Codex: widening any origin shows up here as a one-line diff.
# --------------------------------------------------------------------------- #

def test_every_registered_origin_declares_its_audited_policy():
    """No implicit "any": every origin declares a policy, and this map is the
    reviewable record of which ones passed a Codex acceptance suite.

    The two SDLC origins were widened to ``codex_allowed`` by #1265/#1266 — the
    *option*, not the traffic (``default_engine`` still answers Claude, and both
    the automations kill switch and the network opt-in ship off). Everything else
    is still ``claude_only``: widening one is a one-line diff here."""
    audited = audited_policy_map()
    assert set(audited) == {a.id for a in registry.all()}
    assert audited == {
        "pr-doctor": EnginePolicy.CLAUDE_ONLY,
        "backlog-runner": EnginePolicy.CLAUDE_ONLY,
        "autopilot": EnginePolicy.CLAUDE_ONLY,
        "cicd-doctor": EnginePolicy.CLAUDE_ONLY,
        "archive-audit": EnginePolicy.CLAUDE_ONLY,
        "sdlc-intake": EnginePolicy.CODEX_ALLOWED,
        "sdlc-synthesizer": EnginePolicy.CODEX_ALLOWED,
        "pm-sync": EnginePolicy.CLAUDE_ONLY,
        "pm-triage": EnginePolicy.CLAUDE_ONLY,
        "gh-reconcile": EnginePolicy.CLAUDE_ONLY,
    }


def test_policy_for_defaults_to_claude_only_for_an_object_without_the_attribute():
    """An automation added without declaring a policy is claude_only, never "any"."""

    class _Undeclared:
        id = "not-registered"

    assert policy_for(_Undeclared()) is EnginePolicy.CLAUDE_ONLY


# --------------------------------------------------------------------------- #
# Parsing / vocabulary
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("value,expected", [
    ("claude_only", EnginePolicy.CLAUDE_ONLY),
    ("codex_allowed", EnginePolicy.CODEX_ALLOWED),
    ("codex_only", EnginePolicy.CODEX_ONLY),
    (EnginePolicy.CODEX_ONLY, EnginePolicy.CODEX_ONLY),
])
def test_parse_engine_policy_accepts_the_three_names(value, expected):
    assert parse_engine_policy(value) is expected


@pytest.mark.parametrize("value", [None, "", "any", "claude", "CODEX_ONLY", 7])
def test_parse_engine_policy_rejects_anything_else(value):
    with pytest.raises(ValueError):
        parse_engine_policy(value)


def test_allowed_engines_and_default_engine_per_policy():
    assert allowed_engines(EnginePolicy.CLAUDE_ONLY) == (AgentEngine.CLAUDE_CODE,)
    assert allowed_engines(EnginePolicy.CODEX_ALLOWED) == (
        AgentEngine.CLAUDE_CODE, AgentEngine.CODEX)
    assert allowed_engines(EnginePolicy.CODEX_ONLY) == (AgentEngine.CODEX,)
    # codex_allowed keeps Claude as the default: widening the ceiling must not
    # silently move an origin's traffic onto a new engine.
    assert default_engine(EnginePolicy.CLAUDE_ONLY) is AgentEngine.CLAUDE_CODE
    assert default_engine(EnginePolicy.CODEX_ALLOWED) is AgentEngine.CLAUDE_CODE
    assert default_engine(EnginePolicy.CODEX_ONLY) is AgentEngine.CODEX


# --------------------------------------------------------------------------- #
# Config schema composition
# --------------------------------------------------------------------------- #

def test_claude_only_origin_offers_no_engine_fields():
    """The selector does not exist for an unaudited origin, so `validate_config`
    rejects `engine` as an unknown key — the API's 422 comes for free."""
    assert engine_config_fields(EnginePolicy.CLAUDE_ONLY) == []


def test_codex_allowed_origin_offers_engine_and_codex_launch_fields():
    fields = {f.key: f for f in engine_config_fields(EnginePolicy.CODEX_ALLOWED)}
    assert set(fields) == {ENGINE_CONFIG_KEY, "codex_model", "codex_reasoning_effort"}
    engine_field = fields[ENGINE_CONFIG_KEY]
    assert engine_field.type == "enum"
    assert engine_field.choices == ["claude_code", "codex"]
    assert engine_field.default == "claude_code"
    # Codex model/effort are free-text: the accepted pairs come from the live
    # per-account catalog, so a frozen enum here would go stale.
    assert fields["codex_model"].type == "string"
    assert fields["codex_model"].default == ""


def test_codex_only_origin_offers_codex_as_the_sole_choice():
    fields = {f.key: f for f in engine_config_fields(EnginePolicy.CODEX_ONLY)}
    assert fields[ENGINE_CONFIG_KEY].choices == ["codex"]
    assert fields[ENGINE_CONFIG_KEY].default == "codex"


def test_effective_config_schema_appends_engine_fields_without_dropping_any():
    backlog = registry.get("backlog-runner")
    own = [f.key for f in backlog.config_schema]
    effective = [f.key for f in effective_config_schema(backlog)]
    assert effective[:len(own)] == own
    # claude_only today, so nothing is appended.
    assert effective == own


# --------------------------------------------------------------------------- #
# resolve_engine — the negative tests the plan names
# --------------------------------------------------------------------------- #

def test_unaudited_origin_cannot_choose_codex_even_with_a_forged_config_row():
    """Defense in depth: `validate_config` already rejects the key, so reaching
    here means the row was written out of band. The spawn still runs Claude."""
    assert resolve_engine(
        EnginePolicy.CLAUDE_ONLY, {ENGINE_CONFIG_KEY: "codex"},
        automations_enabled=True) is AgentEngine.CLAUDE_CODE


def test_unaudited_origin_cannot_choose_codex_even_when_the_flag_is_on():
    assert resolve_engine(
        EnginePolicy.CLAUDE_ONLY, {}, automations_enabled=True
    ) is AgentEngine.CLAUDE_CODE


def test_codex_allowed_origin_runs_claude_until_the_config_opts_in():
    assert resolve_engine(
        EnginePolicy.CODEX_ALLOWED, {}, automations_enabled=True
    ) is AgentEngine.CLAUDE_CODE
    assert resolve_engine(
        EnginePolicy.CODEX_ALLOWED, {ENGINE_CONFIG_KEY: "codex"},
        automations_enabled=True) is AgentEngine.CODEX


def test_the_kill_switch_forces_an_opted_in_origin_back_to_claude():
    """`CCSWITCH_CODEX_AUTOMATIONS_ENABLED=false` must disable Codex automation
    globally without an operator having to unwind every per-origin config."""
    assert resolve_engine(
        EnginePolicy.CODEX_ALLOWED, {ENGINE_CONFIG_KEY: "codex"},
        automations_enabled=False) is AgentEngine.CLAUDE_CODE


def test_a_codex_only_origin_refuses_to_spawn_when_the_flag_is_off():
    """It must NOT silently downgrade: an origin audited for Codex ALONE has no
    Claude acceptance evidence, so running it on Claude is the unsafe outcome."""
    with pytest.raises(EnginePolicyViolation):
        resolve_engine(EnginePolicy.CODEX_ONLY, {}, automations_enabled=False)


def test_a_codex_only_origin_refuses_a_config_that_names_claude():
    with pytest.raises(EnginePolicyViolation):
        resolve_engine(EnginePolicy.CODEX_ONLY, {ENGINE_CONFIG_KEY: "claude_code"},
                       automations_enabled=True)


def test_an_unparseable_engine_value_falls_back_to_the_policy_default():
    assert resolve_engine(
        EnginePolicy.CODEX_ALLOWED, {ENGINE_CONFIG_KEY: "gpt-9"},
        automations_enabled=True) is AgentEngine.CLAUDE_CODE


# --------------------------------------------------------------------------- #
# sanitize_launch_for_engine — "do not silently route a helper call to the
# automation's account", expressed at the launch boundary.
# --------------------------------------------------------------------------- #

def test_a_claude_launch_is_passed_through_untouched():
    kwargs = {"account": "tony_cairnea.com", "model": "opus", "name": "x"}
    out, dropped = sanitize_launch_for_engine(
        AgentEngine.CLAUDE_CODE, dict(kwargs), config={},
        provider_of=lambda _n: "anthropic")
    assert out == kwargs
    assert dropped == ()


def test_a_codex_launch_drops_a_claude_account_and_a_claude_model_alias():
    """`resolve_model` is a Claude-only complexity assessor picking Claude
    aliases, and `_backlog_healthy_account` returns an Anthropic account. Handing
    either to a Codex spawn is the silent mis-route Task 7.1 forbids."""
    out, dropped = sanitize_launch_for_engine(
        AgentEngine.CODEX, {"account": "tony_cairnea.com", "model": "opus", "name": "x"},
        config={}, provider_of=lambda _n: "anthropic")
    assert out == {"account": None, "name": "x"}
    assert set(dropped) == {"account", "model"}


def test_a_codex_launch_keeps_an_openai_account():
    out, dropped = sanitize_launch_for_engine(
        AgentEngine.CODEX, {"account": "tony_openai", "model": "opus"},
        config={}, provider_of=lambda _n: "openai")
    assert out["account"] == "tony_openai"
    assert dropped == ("model",)


def test_a_codex_launch_takes_model_and_effort_from_the_codex_config_keys():
    out, _dropped = sanitize_launch_for_engine(
        AgentEngine.CODEX, {"account": None, "model": "opus"},
        config={"codex_model": "gpt-5-codex", "codex_reasoning_effort": "high"},
        provider_of=lambda _n: "openai")
    assert out["model"] == "gpt-5-codex"
    assert out["reasoning_effort"] == "high"


def test_blank_codex_config_keys_mean_engine_default_not_empty_string():
    out, _dropped = sanitize_launch_for_engine(
        AgentEngine.CODEX, {"account": None},
        config={"codex_model": "", "codex_reasoning_effort": "  "},
        provider_of=lambda _n: "openai")
    assert "model" not in out
    assert "reasoning_effort" not in out


def test_an_unreadable_account_provider_is_treated_as_incompatible():
    """Fail closed: an account whose provider cannot be read must not be handed
    to a Codex runner on the assumption that it is an OpenAI one."""
    def _boom(_name):
        raise RuntimeError("ledger unreadable")

    out, dropped = sanitize_launch_for_engine(
        AgentEngine.CODEX, {"account": "mystery"}, config={}, provider_of=_boom)
    assert out["account"] is None
    assert "account" in dropped
