"""Pure config-schema validation + status value type."""
from __future__ import annotations

import pytest


def _schema():
    from server.automations.base import ConfigField
    return [
        ConfigField(key="enabled_repos", label="Repos", type="repo-multiselect", default=[]),
        ConfigField(key="max_attempts", label="Max attempts", type="int", default=3),
        ConfigField(key="lens", label="Lens", type="enum", default="a",
                    help="", choices=["a", "b"]),
        ConfigField(key="repo_weights", label="Weights", type="repo-number-map",
                    default={}),
    ]


def test_validate_config_accepts_and_coerces():
    from server.automations.base import validate_config
    out = validate_config(_schema(), {"max_attempts": 5, "enabled_repos": ["x/y"]})
    assert out["max_attempts"] == 5
    assert out["enabled_repos"] == ["x/y"]
    assert out["lens"] == "a"  # default filled


def test_validate_config_rejects_unknown_key():
    from server.automations.base import validate_config
    with pytest.raises(ValueError, match="unknown config key"):
        validate_config(_schema(), {"nope": 1})


def test_validate_config_rejects_wrong_type():
    from server.automations.base import validate_config
    with pytest.raises(ValueError, match="max_attempts"):
        validate_config(_schema(), {"max_attempts": "five"})


def test_validate_config_rejects_bad_enum():
    from server.automations.base import validate_config
    with pytest.raises(ValueError, match="lens"):
        validate_config(_schema(), {"lens": "z"})


def test_repo_number_map_accepts_str_to_number():
    from server.automations.base import validate_config
    out = validate_config(_schema(), {"repo_weights": {"o/a": 3, "o/b": 1.5, "o/c": 0}})
    assert out["repo_weights"] == {"o/a": 3, "o/b": 1.5, "o/c": 0}


def test_repo_number_map_default_is_empty_dict():
    from server.automations.base import validate_config
    out = validate_config(_schema(), {})
    assert out["repo_weights"] == {}


def test_repo_number_map_rejects_non_dict():
    from server.automations.base import validate_config
    with pytest.raises(ValueError, match="repo_weights"):
        validate_config(_schema(), {"repo_weights": ["o/a"]})


def test_repo_number_map_rejects_non_number_value():
    from server.automations.base import validate_config
    with pytest.raises(ValueError, match="repo_weights"):
        validate_config(_schema(), {"repo_weights": {"o/a": "3"}})


def test_repo_number_map_rejects_bool_value():
    # bool is an int subclass — a stray True must not read as the number 1.
    from server.automations.base import validate_config
    with pytest.raises(ValueError, match="repo_weights"):
        validate_config(_schema(), {"repo_weights": {"o/a": True}})


def test_status_to_dict():
    from server.automations.base import AutomationStatus
    s = AutomationStatus(text="watching 3 PRs", level="active", counts={"failing": 3})
    assert s.to_dict() == {"text": "watching 3 PRs", "level": "active", "counts": {"failing": 3}}
