"""The pr-doctor reference automation: identity, modes-from-source, status."""
from __future__ import annotations


def test_modes_sourced_from_valid_autofix_modes():
    from server.automations.pr_doctor import PrDoctor
    from server.sessions.pr_fix import VALID_AUTOFIX_MODES
    pd = PrDoctor()
    assert set(pd.modes) == set(VALID_AUTOFIX_MODES)
    assert pd.modes[0] == "off"  # off renders first in the dropdown
    assert pd.id == "pr-doctor" and pd.name == "PR Doctor"


def test_config_schema_has_repo_filter():
    from server.automations.pr_doctor import PrDoctor
    keys = {f.key: f for f in PrDoctor().config_schema}
    assert keys["enabled_repos"].type == "repo-multiselect"


def test_status_is_cheap_and_idle_by_default():
    from server.automations.pr_doctor import PrDoctor
    s = PrDoctor().status(ctx=None)  # must not touch I/O
    assert s.level in ("idle", "active") and isinstance(s.text, str)


def test_registered_in_registry():
    from server.automations.registry import registry
    assert "pr-doctor" in [a.id for a in registry.all()]


def test_config_schema_v1_fields():
    from server.automations.pr_doctor import PrDoctor
    keys = {f.key: f for f in PrDoctor().config_schema}
    assert keys["enabled_repos"].type == "repo-multiselect"
    assert keys["max_attempts"].type == "int" and keys["max_attempts"].default == 3
    assert keys["mechanical_attempts"].type == "int" and keys["mechanical_attempts"].default == 2
    assert keys["auto_update_out_of_date"].type == "bool" and keys["auto_update_out_of_date"].default is True
    assert keys["auto_fix_failing_ci"].default is True
    assert keys["auto_resolve_conflicts"].default is False
    # stuck_checks deferred to v2 — no such field
    assert "auto_rerun_stuck_checks" not in keys
