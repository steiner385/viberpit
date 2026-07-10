"""Per-automation session-model selection: the assessor's fail-open contract,
resolve_model's config mapping, and the default_model schema on both spawning
automations."""
from __future__ import annotations

import pytest

from server.automations.base import validate_config
from server.automations.model_pick import (
    AUTO_MODEL_POOL,
    AUTOMATION_MODEL_CHOICES,
    assess_model,
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
