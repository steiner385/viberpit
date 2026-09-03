"""BacklogRunner automation: identity, config schema, and registry presence."""
from __future__ import annotations


def test_id_and_name():
    from server.automations.backlog_runner import BacklogRunner
    br = BacklogRunner()
    assert br.id == "backlog-runner"
    assert br.name == "Backlog Runner"


def test_modes():
    from server.automations.backlog_runner import BacklogRunner
    assert BacklogRunner().modes == ("off", "propose", "auto")


def test_config_schema_keys_and_types():
    from server.automations.backlog_runner import BacklogRunner
    keys = {f.key: f for f in BacklogRunner().config_schema}
    assert keys["enabled_repos"].type == "repo-multiselect"
    assert keys["enabled_repos"].default == []
    assert keys["eligible_label"].type == "string"
    assert keys["eligible_label"].default == "ready"
    assert keys["max_concurrent"].type == "int"
    assert keys["max_concurrent"].default == 1
    assert keys["max_spawns_per_day"].type == "int"
    assert keys["max_spawns_per_day"].default == 10
    assert keys["max_attempts"].type == "int"
    assert keys["max_attempts"].default == 2
    assert keys["max_issues_per_pr"].type == "int"
    assert keys["max_issues_per_pr"].default == 1


def test_status_idle_by_default():
    from server.automations.backlog_runner import BacklogRunner
    s = BacklogRunner().status(ctx=None)
    assert s.level == "idle"
    assert isinstance(s.text, str)


def test_registered_in_registry():
    from server.automations.registry import registry
    assert "backlog-runner" in [a.id for a in registry.all()]


def test_config_schema_respect_dependencies_defaults_on():
    from server.automations.backlog_runner import BacklogRunner
    keys = {f.key: f for f in BacklogRunner().config_schema}
    assert keys["respect_dependencies"].type == "bool"
    assert keys["respect_dependencies"].default is True


def test_status_shows_blocked_count():
    from types import SimpleNamespace

    from server.automations.backlog_runner import BacklogRunner
    ctx = SimpleNamespace(backlog_counts={"in_flight": 1, "eligible": 2, "blocked": 3})
    assert "3 blocked" in BacklogRunner().status(ctx).text


def test_status_omits_blocked_when_zero():
    from types import SimpleNamespace

    from server.automations.backlog_runner import BacklogRunner
    ctx = SimpleNamespace(backlog_counts={"in_flight": 1, "eligible": 2, "blocked": 0})
    assert "blocked" not in BacklogRunner().status(ctx).text
