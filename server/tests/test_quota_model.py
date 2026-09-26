"""quota_model: the key a session's model is matched against quota windows with."""
import json

import pytest

from server.sessions.engine import AgentEngine
from server.sessions.model_policy import quota_model

from viberpit import paths


@pytest.mark.parametrize("model, expected", [
    ("fable", "fable"),
    ("fable[1m]", "fable"),
    ("claude-fable-5-1", "fable"),
    ("Opus", "opus"),
    ("opusplan", "opus"),
    ("claude-haiku-4-5-20251001", "haiku"),
    ("best", None),                 # no fixed tier: every window applies
    ("some-unknown-id", None),
])
def test_explicit_models_map_to_their_tier(model, expected, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    assert quota_model(model) == expected


def test_default_resolves_to_the_inherited_fleet_model(monkeypatch, tmp_path):
    """A 'Default' session launches on the shared settings model (#1460), so it is
    judged on that model's windows — with the fleet default on Fable, a Fable-
    exhausted account must not receive a Default session."""
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    (tmp_path / "settings.json").write_text(json.dumps({"model": "fable"}))
    monkeypatch.setattr(paths, "shared_base", lambda: tmp_path)
    assert quota_model(None) == "fable"
    assert quota_model("default") == "fable"


def test_default_with_nothing_pinned_is_indeterminate(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    monkeypatch.setattr(paths, "shared_base", lambda: tmp_path)   # no settings files
    assert quota_model(None) is None


def test_non_claude_engines_are_model_blind():
    assert quota_model("gpt-5-codex", engine=AgentEngine.CODEX) is None
    assert quota_model("fable", engine="codex") is None
