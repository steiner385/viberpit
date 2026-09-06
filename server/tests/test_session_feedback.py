from __future__ import annotations


def test_session_tools_flags_default(monkeypatch):
    from server.config import Settings
    monkeypatch.delenv("CCSWITCH_SESSION_TOOLS_ENABLED", raising=False)
    monkeypatch.delenv("CCSWITCH_SESSION_FEEDBACK_CAP", raising=False)
    s = Settings()
    assert s.session_tools_enabled is True
    assert s.session_feedback_cap == 3


def test_session_tools_flags_from_env(monkeypatch):
    from server.config import Settings
    monkeypatch.setenv("CCSWITCH_SESSION_TOOLS_ENABLED", "false")
    monkeypatch.setenv("CCSWITCH_SESSION_FEEDBACK_CAP", "7")
    s = Settings()
    assert s.session_tools_enabled is False
    assert s.session_feedback_cap == 7


def test_flags_are_listed_on_about():
    from server.api.settings import _MANAGED
    keys = {row[0] for row in _MANAGED}
    assert {"session_tools_enabled", "session_feedback_cap"} <= keys
