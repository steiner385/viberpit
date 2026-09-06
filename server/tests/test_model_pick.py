"""Per-automation session-model selection: the assessor's fail-open contract,
resolve_model's config mapping, and the default_model schema on both spawning
automations."""
from __future__ import annotations

import pytest

from server.automations.base import validate_config
from server.automations.model_pick import (
    AUTO_MODEL_POOL,
    AUTOMATION_MODEL_CHOICES,
    assess_codex_effort,
    assess_model,
    resolve_codex_model,
    resolve_model,
)
from server.sessions.model_catalog import ALIASES


def _fake_query(reply_text):
    async def gen(*, prompt, options):
        class _Block:
            text = reply_text
        class _Msg:
            content = [_Block()]
        yield _Msg()
    return gen


# --------------------------------------------------------------------------- #
# choices contract                                                             #
# --------------------------------------------------------------------------- #

def test_choices_are_auto_plus_catalog_aliases():
    # Single source of truth: the dropdown is exactly the binary's /model
    # picker list with "auto" prepended (ALIASES already leads with "default").
    assert AUTOMATION_MODEL_CHOICES == ["auto", *ALIASES]
    assert AUTOMATION_MODEL_CHOICES[0] == "auto"
    assert "default" in AUTOMATION_MODEL_CHOICES
    assert set(AUTO_MODEL_POOL) <= set(ALIASES)


# --------------------------------------------------------------------------- #
# assess_model                                                                 #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
@pytest.mark.parametrize("tier", AUTO_MODEL_POOL)
async def test_assess_returns_each_pool_tier(tier):
    got = await assess_model("fix a bug", _query_fn=_fake_query(tier),
                             _config_dir="", _cwd="/tmp")
    assert got == tier


@pytest.mark.asyncio
async def test_assess_tolerates_case_whitespace_and_period():
    got = await assess_model("refactor everything",
                             _query_fn=_fake_query("  Opus. \n"),
                             _config_dir="", _cwd="/tmp")
    assert got == "opus"


@pytest.mark.asyncio
async def test_assess_out_of_pool_reply_is_none():
    # "default" and "best" are valid catalog aliases but NOT auto tiers.
    got = await assess_model("do things", _query_fn=_fake_query("best"),
                             _config_dir="", _cwd="/tmp")
    assert got is None


@pytest.mark.asyncio
async def test_assess_garbage_reply_is_none():
    got = await assess_model("do things",
                             _query_fn=_fake_query("I think sonnet is right"),
                             _config_dir="", _cwd="/tmp")
    assert got is None


@pytest.mark.asyncio
async def test_assess_model_raising_is_none():
    async def boom(*, prompt, options):
        raise RuntimeError("model down")
        yield  # pragma: no cover
    got = await assess_model("do things", _query_fn=boom,
                             _config_dir="", _cwd="/tmp")
    assert got is None


@pytest.mark.asyncio
async def test_assess_times_out_to_none():
    # A wedged/slow model call must fail open to the account default rather
    # than hang the inline automation spawn pipeline (issue #567).
    import asyncio

    async def hang(*, prompt, options):
        await asyncio.sleep(10)
        yield  # pragma: no cover

    got = await assess_model("do things", _query_fn=hang,
                             _config_dir="", _cwd="/tmp", _timeout=0.05)
    assert got is None


@pytest.mark.asyncio
async def test_assess_empty_summary_skips_the_model():
    async def never(*, prompt, options):
        raise AssertionError("must not be called")
        yield  # pragma: no cover
    assert await assess_model("   ", _query_fn=never) is None


@pytest.mark.asyncio
async def test_assess_without_query_fn_fails_open():
    # The conftest guard (and, in production, an unimportable SDK) must yield
    # None — never an exception that would fail the spawn.
    assert await assess_model("anything") is None


@pytest.mark.asyncio
async def test_assess_uses_pick_helper_config_dir_when_no_config_dir_given():
    """Daemon path: no _config_dir → assess_model calls pick_helper_config_dir."""
    from unittest.mock import patch

    patched_dir = "/fake/cfg/daemon-account"
    with patch("server.automations.model_pick.pick_helper_config_dir", return_value=patched_dir) as mock_pick:
        got = await assess_model("fix a bug", _query_fn=_fake_query("sonnet"), _cwd="/tmp")
    mock_pick.assert_called_once()
    assert got == "sonnet"


# --------------------------------------------------------------------------- #
# resolve_model                                                                #
# --------------------------------------------------------------------------- #

def _never_assess():
    async def never(task_summary):
        raise AssertionError("assessor must not be consulted")
    return never


@pytest.mark.asyncio
async def test_resolve_default_is_account_default():
    got = await resolve_model({"default_model": "default"}, "job",
                              _assess=_never_assess())
    assert got == (None, None)


@pytest.mark.asyncio
async def test_resolve_pinned_alias_passes_through():
    got = await resolve_model({"default_model": "opus"}, "job",
                              _assess=_never_assess())
    assert got == ("opus", "pinned")


@pytest.mark.asyncio
async def test_resolve_auto_uses_assessment():
    async def pick_fable(task_summary):
        assert "hard job" in task_summary
        return "fable"
    got = await resolve_model({"default_model": "auto"}, "hard job",
                              _assess=pick_fable)
    assert got == ("fable", "auto")


@pytest.mark.asyncio
async def test_resolve_auto_assessment_failure_falls_back():
    async def no_pick(task_summary):
        return None
    got = await resolve_model({"default_model": "auto"}, "job", _assess=no_pick)
    assert got == (None, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["gpt-5", 3, None])
async def test_resolve_unknown_or_missing_value_is_account_default(value):
    config = {} if value is None else {"default_model": value}
    got = await resolve_model(config, "job", _assess=_never_assess())
    assert got == (None, None)


# --------------------------------------------------------------------------- #
# default_model schema on the two spawning automations                         #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("automation_cls", ["BacklogRunner", "PrDoctor"])
def test_spawning_automations_carry_default_model(automation_cls):
    if automation_cls == "BacklogRunner":
        from server.automations.backlog_runner import BacklogRunner as cls
    else:
        from server.automations.pr_doctor import PrDoctor as cls
    field = {f.key: f for f in cls.config_schema}["default_model"]
    assert field.type == "enum"
    assert field.default == "auto"
    assert field.choices == AUTOMATION_MODEL_CHOICES

    # validate_config fills the default and rejects out-of-catalog values.
    assert validate_config(cls.config_schema, {})["default_model"] == "auto"
    with pytest.raises(ValueError):
        validate_config(cls.config_schema, {"default_model": "gpt-5"})


def test_non_spawning_automations_do_not_carry_default_model():
    # Autopilot / CI/CD Doctor / Archive Audit never spawn sessions — a model
    # setting there would be dead config (constitution Principle VI).
    from server.automations.archive_audit import ArchiveAudit
    from server.automations.autopilot import Autopilot
    from server.automations.cicd_doctor import CicdDoctor
    for cls in (Autopilot, CicdDoctor, ArchiveAudit):
        assert "default_model" not in {f.key for f in cls.config_schema}


# --------------------------------------------------------------------------- #
# assess_codex_effort / resolve_codex_model — issue #1289                     #
# --------------------------------------------------------------------------- #

_CODEX_EFFORTS = ("minimal", "low", "medium", "high")


@pytest.mark.asyncio
async def test_assess_codex_effort_maps_cheapest_tier_to_cheapest_effort():
    async def pick_haiku(task_summary):
        return "haiku"  # cheapest AUTO_MODEL_POOL tier
    got = await assess_codex_effort("typo fix", _CODEX_EFFORTS, _assess=pick_haiku)
    assert got == "minimal"


@pytest.mark.asyncio
async def test_assess_codex_effort_maps_hardest_tier_to_hardest_effort():
    async def pick_fable(task_summary):
        return "fable"  # hardest AUTO_MODEL_POOL tier
    got = await assess_codex_effort("huge refactor", _CODEX_EFFORTS, _assess=pick_fable)
    assert got == "high"


@pytest.mark.asyncio
async def test_assess_codex_effort_never_returns_a_claude_alias():
    # The mapped result must always come FROM the caller-supplied catalog
    # slice, never from AUTO_MODEL_POOL/ALIASES — the leak this issue guards.
    for tier in AUTO_MODEL_POOL:
        async def pick(task_summary, _tier=tier):
            return _tier
        got = await assess_codex_effort("job", _CODEX_EFFORTS, _assess=pick)
        assert got in _CODEX_EFFORTS
        assert got not in AUTO_MODEL_POOL


@pytest.mark.asyncio
async def test_assess_codex_effort_empty_catalog_is_none():
    async def never(task_summary):
        raise AssertionError("must not consult the assessor with nothing to map onto")
    assert await assess_codex_effort("job", (), _assess=never) is None


@pytest.mark.asyncio
async def test_assess_codex_effort_assessment_failure_is_none():
    async def no_pick(task_summary):
        return None
    got = await assess_codex_effort("job", _CODEX_EFFORTS, _assess=no_pick)
    assert got is None


@pytest.mark.asyncio
async def test_resolve_codex_model_auto_uses_assessment():
    async def pick_medium(task_summary, efforts):
        assert efforts == _CODEX_EFFORTS
        return "medium"
    got = await resolve_codex_model(
        {"default_model": "auto"}, "job", _CODEX_EFFORTS, _assess=pick_medium)
    assert got == ("medium", "auto")


@pytest.mark.asyncio
async def test_resolve_codex_model_assessment_failure_falls_back():
    async def no_pick(task_summary, efforts):
        return None
    got = await resolve_codex_model(
        {"default_model": "auto"}, "job", _CODEX_EFFORTS, _assess=no_pick)
    assert got == (None, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["default", "opus", "gpt-5", None])
async def test_resolve_codex_model_non_auto_never_consults_assessor(value):
    # A pinned Claude alias or "default" must never reach the Codex assessor —
    # Codex pins go through its own codex_model/codex_reasoning_effort config.
    async def never(task_summary, efforts):
        raise AssertionError("assessor must not be consulted")
    config = {} if value is None else {"default_model": value}
    got = await resolve_codex_model(config, "job", _CODEX_EFFORTS, _assess=never)
    assert got == (None, None)
