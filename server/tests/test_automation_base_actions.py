from server.automations.base import ActionSpec, ActionableItem, TriggerResult


def test_action_spec_defaults_to_non_risky():
    s = ActionSpec(kind="failing_ci", label="Fix CI")
    assert s.style == "default"


def test_actionable_item_to_dict_nests_actions():
    item = ActionableItem(
        id="owner/repo#7", title="red CI", url="https://x/7",
        actions=[ActionSpec(kind="failing_ci", label="Fix CI", style="default")])
    assert item.to_dict() == {
        "id": "owner/repo#7", "title": "red CI", "subtitle": None, "url": "https://x/7",
        "actions": [{"kind": "failing_ci", "label": "Fix CI", "style": "default"}],
    }


def test_trigger_result_to_dict():
    r = TriggerResult(status="spawned", level="ok", message="Spawned fix session")
    assert r.to_dict() == {"status": "spawned", "level": "ok", "message": "Spawned fix session"}
