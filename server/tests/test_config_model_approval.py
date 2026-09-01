"""Tests for model-approval gate config settings."""
from server.config import Settings


def test_defaults():
    s = Settings()
    assert s.model_approval_action == "require_approval"
    assert s.model_approval_denylist == "fable"


def test_env_override(monkeypatch):
    monkeypatch.setenv("CCSWITCH_MODEL_APPROVAL_ACTION", "block")
    monkeypatch.setenv("CCSWITCH_MODEL_APPROVAL_DENYLIST", "fable,haiku")
    s = Settings()
    assert s.model_approval_action == "block"
    assert s.model_approval_denylist == "fable,haiku"
