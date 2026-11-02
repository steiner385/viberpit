from server.sessions.permissions import session_grant_effective

OK = {"pattern": "Bash(npm test)", "level": "ok"}
WARN = {"pattern": "Bash(*)", "level": "warn"}


def test_claude_card_needs_an_ok_suggestion():
    assert session_grant_effective([OK], False) is True
    assert session_grant_effective([WARN], False) is False
    assert session_grant_effective([], False) is False


def test_engine_managed_card_always_grants():
    assert session_grant_effective([], True) is True


def test_rules_kill_switch_degrades_a_claude_card(monkeypatch):
    monkeypatch.setenv("VIBERPIT_PERMISSION_RULES_ENABLED", "false")
    assert session_grant_effective([OK], False) is False
    assert session_grant_effective([], True) is True
