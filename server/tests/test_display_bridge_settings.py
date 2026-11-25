"""Display-bridge settings: defaults and env override in BOTH Settings classes."""
from __future__ import annotations


import pytest

import server.config as cfg

_DEFAULTS = {
    "display_bridge_enabled": True,
    "display_bridge_accept_from": "",
    "display_bridge_x11": True,
    "display_bridge_start_timeout_s": 15.0,
    "display_bridge_grace_s": 60.0,
}
_ENV = {
    "display_bridge_enabled": "VIBERPIT_DISPLAY_BRIDGE_ENABLED",
    "display_bridge_accept_from": "VIBERPIT_DISPLAY_BRIDGE_ACCEPT_FROM",
    "display_bridge_x11": "VIBERPIT_DISPLAY_BRIDGE_X11",
    "display_bridge_start_timeout_s": "VIBERPIT_DISPLAY_BRIDGE_START_TIMEOUT_S",
    "display_bridge_grace_s": "VIBERPIT_DISPLAY_BRIDGE_GRACE_S",
}




@pytest.fixture
def clean_env(monkeypatch):
    for v in _ENV.values():
        monkeypatch.delenv(v, raising=False)


def test_settings_defaults(clean_env):
    s = cfg.get_settings()
    for k, v in _DEFAULTS.items():
        assert getattr(s, k) == v, k


def test_settings_env_override(clean_env, monkeypatch):
    monkeypatch.setenv("VIBERPIT_DISPLAY_BRIDGE_ENABLED", "false")
    monkeypatch.setenv("VIBERPIT_DISPLAY_BRIDGE_X11", "false")
    s = cfg.get_settings()
    assert s.display_bridge_enabled is False
    assert s.display_bridge_x11 is False


def test_bridge_x_start_setting_is_gone():
    """The X11 forward is a path display in the private dir (spec §8 fix round 1): no
    display number to configure, so no VIBERPIT_DISPLAY_BRIDGE_X_START (Principle VI)."""
    import pathlib
    assert not hasattr(cfg.get_settings(), "display_bridge_x_start")
    assert "DISPLAY_BRIDGE_X_START" not in pathlib.Path(cfg.__file__).read_text()


def test_both_settings_classes_declare_the_fields():
    """The pydantic class and the dataclass fallback live in one if/else; the unused branch
    is not importable, so check the source declares each name + env var twice."""
    import pathlib
    src = pathlib.Path(cfg.__file__).read_text()
    for field, env in _ENV.items():
        assert src.count(f"{field}:") == 2, field
        assert src.count(env) == 2, env
