"""CicdDoctor identity, config schema, status rendering, registration."""
from __future__ import annotations

from types import SimpleNamespace

from server.automations.base import validate_config
from server.automations.cicd_doctor import CicdDoctor
from server.automations.registry import registry


def test_registered_with_stable_slug():
    assert registry.get("cicd-doctor").__class__ is CicdDoctor


def test_config_schema_validates_defaults():
    cfg = validate_config(CicdDoctor.config_schema, {})
    assert cfg["min_window_days"] == 21 and cfg["min_runs"] == 30
    assert cfg["max_flake_pct"] == 2 and cfg["max_p95_minutes"] == 10
    assert cfg["demote_flake_pct"] == 5 and cfg["demote_window_days"] == 7
    assert cfg["retire_days"] == 60 and cfg["flaky_test_min_pct"] == 2
    assert cfg["retention_days"] == 90
    assert cfg["file_issues"] is True and cfg["enabled_repos"] == []


def test_status_levels():
    d = CicdDoctor()
    assert d.status(None).level == "idle"
    ctx = SimpleNamespace(cicd_doctor_counts={"open": 3, "candidates": 1})
    st = d.status(ctx)
    assert st.level == "active" and "3 open" in st.text and "1 graduation" in st.text
    quiet = SimpleNamespace(cicd_doctor_counts={"open": 0, "candidates": 0})
    assert d.status(quiet).level == "idle"
