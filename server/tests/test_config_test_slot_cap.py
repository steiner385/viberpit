"""CCSWITCH_TEST_SLOT_CAP — verification-phase test-run slot cap (#822)."""
from __future__ import annotations


def test_default_cap_is_one(monkeypatch):
    monkeypatch.delenv("CCSWITCH_TEST_SLOT_CAP", raising=False)
    from server.config import get_settings
    assert get_settings().test_slot_cap == 1


def test_cap_reads_from_env(monkeypatch):
    monkeypatch.setenv("CCSWITCH_TEST_SLOT_CAP", "3")
    from server.config import get_settings
    assert get_settings().test_slot_cap == 3


def test_bad_cap_value_falls_back_to_default_on_dataclass_path(monkeypatch):
    """Only meaningful on the pydantic-settings-unavailable fallback path (see
    server/config.py's ``except ImportError`` block) — a non-digit string there
    is guarded explicitly (mirrors CCSWITCH_SELF_MODEL_SWITCH_CAP). Under normal
    pydantic-settings installs a non-int value raises at Settings() construction,
    which is also an acceptable (loud) failure mode for a malformed operator env.
    """
    import server.config as config_mod
    if not hasattr(config_mod, "BaseSettings"):
        monkeypatch.setenv("CCSWITCH_TEST_SLOT_CAP", "not-a-number")
        assert config_mod.get_settings().test_slot_cap == 1
