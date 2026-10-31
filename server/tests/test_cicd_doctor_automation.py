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


def test_status_errors():
    d = CicdDoctor()
    # errors with no open findings → warn (doctor is partially blind)
    err_ctx = SimpleNamespace(cicd_doctor_counts={"open": 0, "candidates": 0, "errors": 2})
    st = d.status(err_ctx)
    assert st.level == "warn" and "2 repo error(s)" in st.text
    # errors with open findings → active wins (but error count still visible in text)
    both_ctx = SimpleNamespace(cicd_doctor_counts={"open": 1, "candidates": 0, "errors": 1})
    st2 = d.status(both_ctx)
    assert st2.level == "active" and "1 repo error(s)" in st2.text


def test_modes_include_correct():
    assert CicdDoctor.modes == ("off", "propose", "correct")


def test_systemic_config_fields_and_defaults():
    cfg = validate_config(CicdDoctor.config_schema, {})
    assert {k: cfg[k] for k in (
        "systemic_window_min", "systemic_min_prs", "systemic_rollup_jobs",
        "systemic_external_feed_jobs", "systemic_quiet_clear_min", "systemic_brake_ttl_min",
        "systemic_brake_max_min", "systemic_dispatch_fix", "systemic_brake",
        "systemic_max_fix_sessions_per_day", "thrash_min_failed_per_hour",
        "aws_alarm_prefix", "aws_profile")} == {
        "systemic_window_min": 60, "systemic_min_prs": 3, "systemic_rollup_jobs": "ci,ci-gate",
        "systemic_external_feed_jobs": "^security: audit$", "systemic_quiet_clear_min": 90,
        "systemic_brake_ttl_min": 240, "systemic_brake_max_min": 600,
        "systemic_dispatch_fix": True, "systemic_brake": True,
        "systemic_max_fix_sessions_per_day": 3, "thrash_min_failed_per_hour": 6,
        "aws_alarm_prefix": "", "aws_profile": ""}
    # The loop's fallback defaults are the same values (one statement each).
    from server.automations.cicd.systemic_loop import DEFAULTS
    assert all(cfg[k] == v for k, v in DEFAULTS.items())


def test_status_reports_systemic_incidents():
    d = CicdDoctor()
    ctx = SimpleNamespace(cicd_doctor_counts={"open": 0, "candidates": 0},
                          cicd_systemic_counts={"incidents": 2, "braked": 1, "thrash": False,
                                                "alarms": 0})
    st = d.status(ctx)
    assert "2 systemic incident(s), 1 braked" in st.text and st.level == "active"
    assert st.counts["systemic_incidents"] == 2 and st.counts["systemic_braked"] == 1
    assert all(isinstance(v, int) for v in st.counts.values())
    # Systemic counts alone (the v1 loop has not ticked yet) still render.
    only = SimpleNamespace(cicd_systemic_counts={"incidents": 1, "braked": 0,
                                                 "thrash": False, "alarms": 0})
    st = d.status(only)
    assert "1 systemic incident(s)" in st.text and "braked" not in st.text
    quiet = SimpleNamespace(cicd_doctor_counts={"open": 0, "candidates": 0},
                            cicd_systemic_counts={"incidents": 0, "braked": 0,
                                                  "thrash": False, "alarms": 0})
    assert "systemic" not in d.status(quiet).text and d.status(quiet).level == "idle"
