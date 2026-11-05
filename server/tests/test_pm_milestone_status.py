from __future__ import annotations

from datetime import datetime, timedelta

from server.pm.milestone_def import Measure
from server.pm.milestone_status import Reading, effective_max_age, evaluate, gate_met, status_at

NOW = datetime(2026, 11, 5, 12, 0, 0)


def _m(kind: str, op: str | None = None, target: float | None = None, max_age_s: int | None = None) -> Measure:
    return Measure(kind, {}, op, target, max_age_s)


def _r(value: dict | None, error: str | None = None) -> Reading:
    return Reading(value, NOW, error, None)


def test_eq_zero_met() -> None:
    assert evaluate(_m("issue_count", "eq", 0), _r({"n": 0})) == "met"


def test_eq_zero_unmet_with_293() -> None:
    assert evaluate(_m("issue_count", "eq", 0), _r({"n": 293})) == "unmet"


def test_ge_streak() -> None:
    m = _m("workflow_streak", "ge", 7)
    assert evaluate(m, _r({"n": 7})) == "met"
    assert evaluate(m, _r({"n": 6})) == "unmet"


def test_all_closed_requires_nonempty() -> None:
    m = _m("sub_issues")
    assert evaluate(m, _r({"done": 0, "total": 0})) == "unmet"
    assert evaluate(m, _r({"done": 3, "total": 3})) == "met"
    assert evaluate(m, _r({"done": 2, "total": 3})) == "unmet"


def test_sub_issues_op_applies_to_open_count() -> None:
    assert evaluate(_m("sub_issues", "le", 1), _r({"done": 2, "total": 3})) == "met"
    assert evaluate(_m("sub_issues", "le", 0), _r({"done": 2, "total": 3})) == "unmet"


def test_error_is_unknown() -> None:
    assert evaluate(_m("issue_count", "eq", 0), _r({"n": 0}, error="boom")) == "unknown"
    assert evaluate(_m("issue_count", "eq", 0), _r(None)) == "unknown"


def test_no_op_is_info() -> None:
    assert evaluate(_m("ci_metric"), _r({"n": 4.2})) == "info"
    assert evaluate(_m("ladder_leaves"), _r({"done": 1, "total": 2, "items": []})) == "info"


def test_manual() -> None:
    m = _m("manual")
    assert evaluate(m, _r({"met": True})) == "met"
    assert evaluate(m, _r({"met": False})) == "unmet"
    assert evaluate(m, _r({"n": 3})) == "info"
    assert evaluate(m, _r(None)) == "unknown"


def test_stale_reads_unknown() -> None:
    measured = NOW - timedelta(hours=37)
    assert status_at("met", measured, 36 * 3600, NOW) == "unknown"
    assert status_at("met", NOW - timedelta(hours=36), 36 * 3600, NOW) == "met"


def test_manual_never_stale() -> None:
    assert status_at("met", NOW - timedelta(days=900), None, NOW) == "met"
    assert status_at("met", None, None, NOW) == "met"


def test_missing_measured_at_with_max_age_is_unknown() -> None:
    assert status_at("met", None, 3600, NOW) == "unknown"


def test_effective_max_age() -> None:
    assert effective_max_age(_m("workflow_streak", "ge", 1)) == 36 * 3600
    assert effective_max_age(_m("workflow_streak", "ge", 1, 60)) == 60
    assert effective_max_age(_m("manual")) is None


def test_gate_met_empty_is_false() -> None:
    assert gate_met([]) is False


def test_gate_met_with_one_unknown_is_false() -> None:
    assert gate_met(["met", "unknown"]) is False
    assert gate_met(["met", "met"]) is True


def test_status_at_mixes_naive_and_aware_times() -> None:
    """SQLite reads timestamptz columns back naive (UTC), Postgres aware: either
    side of the comparison may be either."""
    from datetime import timezone
    aware_now = NOW.replace(tzinfo=timezone.utc)
    fresh = NOW - timedelta(hours=1)
    assert status_at("met", fresh, 3 * 3600, aware_now) == "met"
    assert status_at("met", fresh.replace(tzinfo=timezone.utc), 3 * 3600, NOW) == "met"
    assert status_at("met", NOW - timedelta(hours=4), 3 * 3600, aware_now) == "unknown"
