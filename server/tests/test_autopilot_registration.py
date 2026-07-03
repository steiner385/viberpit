from server.automations.registry import registry
from server.automations.base import validate_config


def test_autopilot_registered():
    a = registry.get("autopilot")
    assert a.id == "autopilot"
    assert a.modes == ("off", "propose", "auto")


def test_config_defaults_validate():
    a = registry.get("autopilot")
    cfg = validate_config(a.config_schema, {})
    assert cfg["allow_tool_approvals"] is True
    assert cfg["max_actions_per_session_per_hour"] == 12
    assert cfg["max_actions_per_day"] == 300
    assert cfg["loop_repeat_limit"] == 3
    assert cfg["human_defer_seconds"] == 120
    assert cfg["extra_deny_patterns"] == ""
