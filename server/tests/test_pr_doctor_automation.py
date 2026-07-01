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
