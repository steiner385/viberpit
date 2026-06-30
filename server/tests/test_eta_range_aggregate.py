# server/tests/test_eta_range_aggregate.py
from datetime import datetime, timezone
from server.pipeline.estimator import compute_progress, ProgressResult
from server.pipeline.history import Expected

NOW = datetime(2026, 6, 29, 12, 10, 0, tzinfo=timezone.utc)


def _lookup(table):
    return lambda n: table.get(n)


def test_aggregate_exposes_low_nominal_high_ordered():
    checks = [{"name": "a", "status": "IN_PROGRESS", "started_at": "2026-06-29T12:09:00Z"}]
    table = {"a": Expected(p10=120.0, p50=300.0, p90=600.0, n=20)}
    r = compute_progress(checks, ["a"], _lookup(table), NOW)
    assert isinstance(r, ProgressResult)
    assert r.eta_low_seconds is not None
    assert r.eta_low_seconds <= r.eta_seconds <= r.eta_high_seconds


def test_overdue_nulls_all_three_bounds():
    # elapsed 10min, p50 60s → overdue → all eta fields None
    checks = [{"name": "a", "status": "IN_PROGRESS", "started_at": "2026-06-29T12:00:00Z"}]
    table = {"a": Expected(p10=20.0, p50=60.0, p90=90.0, n=20)}
    r = compute_progress(checks, ["a"], _lookup(table), NOW)
    assert r.overdue is True
    assert r.eta_seconds is None and r.eta_low_seconds is None and r.eta_high_seconds is None


def test_aggregate_ordering_holds_across_multiple_checks():
    # check "a" dominates the low bound; check "b" dominates nominal/high.
    # Both started ~30s ago and are IN_PROGRESS.
    checks = [
        {"name": "a", "status": "IN_PROGRESS", "started_at": "2026-06-29T12:09:30Z"},
        {"name": "b", "status": "IN_PROGRESS", "started_at": "2026-06-29T12:09:30Z"},
    ]
    table = {
        "a": Expected(p10=200.0, p50=210.0, p90=220.0, n=20),
        "b": Expected(p10=10.0,  p50=400.0, p90=800.0, n=20),
    }
    r = compute_progress(checks, ["a", "b"], _lookup(table), NOW)
    # max-aggregation: eta_low = max(rem_low_a, rem_low_b)  ≤  max(rem_nom_a, rem_nom_b)
    # because the check maximising rem_low also satisfies rem_low ≤ rem_nom ≤ max(rem_nom).
    assert r.eta_low_seconds is not None
    assert r.eta_low_seconds <= r.eta_seconds <= r.eta_high_seconds


def test_all_completed_returns_zero_range():
    checks = [{"name": "a", "status": "COMPLETED"}]
    table = {"a": Expected(p10=1.0, p50=2.0, p90=3.0, n=5)}
    r = compute_progress(checks, ["a"], _lookup(table), NOW)
    assert (r.eta_seconds, r.eta_low_seconds, r.eta_high_seconds) == (0, 0, 0)
