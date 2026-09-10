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


def test_config_schema_queue_min_status_is_the_queue_vocabulary():
    """The operator picks how far down the board the runner reaches; the choices
    are the queue's own (server/pm/queue.py), never a second copy of them."""
    from server.automations.backlog_runner import BacklogRunner
    from server.pm.desired_state import STATUS_READY
    from server.pm.queue import MIN_STATUS_OPTIONS
    field = {f.key: f for f in BacklogRunner().config_schema}["queue_min_status"]
    assert field.type == "enum"
    assert field.default == STATUS_READY
    assert field.choices == list(MIN_STATUS_OPTIONS)


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


# --------------------------------------------------------------------------- #
# Queue provenance on the status card (#990)                                  #
# --------------------------------------------------------------------------- #

def test_status_appends_pick_text_when_present_and_not_idle():
    from types import SimpleNamespace

    from server.automations.backlog_runner import BacklogRunner
    ctx = SimpleNamespace(
        backlog_counts={"in_flight": 1, "eligible": 2, "blocked": 0},
        backlog_last_pick={"ref": "o/r#1", "score": 12.5, "product": "widget",
                           "over": ["o/r#2", "o/r#3"]})
    text = BacklogRunner().status(ctx).text
    assert text.endswith(
        " · picked o/r#1 (score 12.5, product widget) over o/r#2, o/r#3")


def test_status_unchanged_when_no_pick():
    from types import SimpleNamespace

    from server.automations.backlog_runner import BacklogRunner
    ctx = SimpleNamespace(backlog_counts={"in_flight": 1, "eligible": 2, "blocked": 0})
    text = BacklogRunner().status(ctx).text
    assert "picked" not in text


def test_status_omits_pick_when_idle():
    """A stale pick from a prior spawn must not clutter the card once the
    runner has gone idle (0 in flight, nothing needing a human)."""
    from types import SimpleNamespace

    from server.automations.backlog_runner import BacklogRunner
    ctx = SimpleNamespace(
        backlog_counts={"in_flight": 0, "eligible": 2, "blocked": 0, "needs_human": 0},
        backlog_last_pick={"ref": "o/r#1", "score": 12.5, "product": "widget", "over": []})
    status = BacklogRunner().status(ctx)
    assert status.level == "idle"
    assert "picked" not in status.text


def test_format_pick_all_fields():
    from server.automations.backlog_runner import format_pick
    pick = {"ref": "o/r#1", "score": 12.5, "product": "widget",
            "over": ["o/r#2", "o/r#3"]}
    assert format_pick(pick) == "picked o/r#1 (score 12.5, product widget) over o/r#2, o/r#3"


def test_format_pick_score_none():
    from server.automations.backlog_runner import format_pick
    pick = {"ref": "o/r#1", "score": None, "product": "widget", "over": []}
    assert format_pick(pick) == "picked o/r#1 (product widget)"


def test_format_pick_product_none():
    from server.automations.backlog_runner import format_pick
    pick = {"ref": "o/r#1", "score": 5, "product": None, "over": []}
    assert format_pick(pick) == "picked o/r#1 (score 5)"


def test_format_pick_over_empty():
    from server.automations.backlog_runner import format_pick
    pick = {"ref": "o/r#1", "score": 5, "product": "widget", "over": []}
    assert format_pick(pick) == "picked o/r#1 (score 5, product widget)"


def test_format_pick_no_score_no_product_no_over():
    from server.automations.backlog_runner import format_pick
    pick = {"ref": "o/r#1", "score": None, "product": None, "over": []}
    assert format_pick(pick) == "picked o/r#1"
