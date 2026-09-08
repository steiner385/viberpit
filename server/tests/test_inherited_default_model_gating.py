"""A session that names no model must be gated on the model it will ACTUALLY run on.

ccswitch never hands the `claude` binary a model for a "default" launch, so the
binary picks one from the same sources it uses interactively — `ANTHROPIC_MODEL`
in its env, then the `model` key of settings.local.json / settings.json. Every
account's config dir symlinks those two files from `paths.shared_base()`, so one
`/model fable` pick in any session silently became the launch default for every
account, and `resolve_model_policy(model=None)` waved it through as "allow": the
denylist never saw the model that ran (2026-09-08, sessions launched on Fable 5.1
without the approval the denylist demands).

`launch_model()` resolves the inherited default so the existing gate can judge it.
"""
from __future__ import annotations

import json
import os
from unittest.mock import patch

import pytest

from server.sessions import model_policy
from server.sessions.engine import AgentEngine
from server.sessions.model_policy import inherited_default_model, launch_model


@pytest.fixture
def shared_base(tmp_path):
    """A throwaway shared Claude home; paths.shared_base() reads CLAUDE_CONFIG_DIR."""
    base = tmp_path / "dot-claude"
    base.mkdir()
    env = {"CLAUDE_CONFIG_DIR": str(base), "CCSWITCH_HOME": str(tmp_path / "store")}
    with patch.dict(os.environ, env):
        os.environ.pop("ANTHROPIC_MODEL", None)
        yield base


def _write(base, name, payload):
    (base / name).write_text(json.dumps(payload) if not isinstance(payload, str) else payload)


# ---------------------------------------------------------------------------
# inherited_default_model — mirrors the binary's own precedence
# ---------------------------------------------------------------------------

def test_settings_model_key_is_the_inherited_default(shared_base):
    _write(shared_base, "settings.json", {"model": "claude-fable-5-1[1m]"})
    assert inherited_default_model() == "claude-fable-5-1[1m]"


def test_nothing_pinned_means_no_inherited_default(shared_base):
    _write(shared_base, "settings.json", {"permissions": {"allow": ["Bash"]}})
    assert inherited_default_model() is None


def test_missing_settings_file_is_not_an_error(shared_base):
    assert inherited_default_model() is None


def test_unparseable_settings_fail_open_to_no_default(shared_base):
    _write(shared_base, "settings.json", "{not json")
    assert inherited_default_model() is None


def test_non_string_model_value_is_ignored(shared_base):
    _write(shared_base, "settings.json", {"model": {"alias": "fable"}})
    assert inherited_default_model() is None


def test_settings_local_wins_over_settings(shared_base):
    _write(shared_base, "settings.json", {"model": "opus"})
    _write(shared_base, "settings.local.json", {"model": "fable"})
    assert inherited_default_model() == "fable"


def test_settings_env_block_anthropic_model_wins_over_model_key(shared_base):
    _write(shared_base, "settings.json",
           {"model": "opus", "env": {"ANTHROPIC_MODEL": "claude-fable-5-1"}})
    assert inherited_default_model() == "claude-fable-5-1"


def test_process_env_anthropic_model_wins_over_settings(shared_base):
    _write(shared_base, "settings.json", {"model": "opus"})
    with patch.dict(os.environ, {"ANTHROPIC_MODEL": "fable[1m]"}):
        assert inherited_default_model() == "fable[1m]"


# ---------------------------------------------------------------------------
# launch_model — what the gate should judge for a given request
# ---------------------------------------------------------------------------

def test_no_model_resolves_to_the_inherited_default(shared_base):
    _write(shared_base, "settings.json", {"model": "claude-fable-5-1[1m]"})
    assert launch_model(None) == "claude-fable-5-1[1m]"
    assert launch_model("") == "claude-fable-5-1[1m]"


def test_default_alias_resolves_to_the_inherited_default(shared_base):
    """The picker's "Default" row is the same launch as no model at all."""
    _write(shared_base, "settings.json", {"model": "claude-fable-5-1[1m]"})
    assert launch_model("default") == "claude-fable-5-1[1m]"
    assert launch_model("Default") == "claude-fable-5-1[1m]"


def test_explicit_model_is_returned_untouched(shared_base):
    _write(shared_base, "settings.json", {"model": "claude-fable-5-1[1m]"})
    assert launch_model("opus") == "opus"
    assert launch_model("sonnet[1m]") == "sonnet[1m]"


def test_no_model_and_nothing_pinned_stays_none(shared_base):
    assert launch_model(None) is None
    assert launch_model("default") is None


def test_codex_never_inherits_the_claude_settings_default(shared_base):
    """Claude's settings.json says nothing about what a Codex thread runs on."""
    _write(shared_base, "settings.json", {"model": "claude-fable-5-1[1m]"})
    assert launch_model(None, engine=AgentEngine.CODEX) is None
    assert launch_model("gpt-5-codex", engine=AgentEngine.CODEX) == "gpt-5-codex"


# ---------------------------------------------------------------------------
# End to end through the policy: the inherited Fable default is gated like an
# explicit `fable` pick would be.
# ---------------------------------------------------------------------------

class _Row:
    def __init__(self, denylist=None, action=None):
        self.model_approval_denylist = denylist
        self.model_approval_action = action


class _Result:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class _Db:
    def __init__(self, row):
        self._row = row

    async def execute(self, _stmt):
        return _Result(self._row)


@pytest.mark.asyncio
async def test_inherited_fable_default_requires_approval(shared_base):
    _write(shared_base, "settings.json", {"model": "claude-fable-5-1[1m]"})
    policy = await model_policy.resolve_model_policy(
        _Db(_Row("fable", "require_approval")), model=launch_model(None))
    assert policy == "require_approval"


@pytest.mark.asyncio
async def test_inherited_ungated_default_is_allowed(shared_base):
    _write(shared_base, "settings.json", {"model": "opus"})
    policy = await model_policy.resolve_model_policy(
        _Db(_Row("fable", "require_approval")), model=launch_model(None))
    assert policy == "allow"
