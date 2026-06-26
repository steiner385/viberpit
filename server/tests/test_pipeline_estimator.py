from datetime import datetime, timezone
from server.pipeline.estimator import compute_progress, ProgressResult
from server.pipeline.history import Expected

NOW = datetime(2026, 6, 25, 12, 0, 0, tzinfo=timezone.utc)


def _lookup(table):
    return lambda n: table.get(n)


def test_all_complete_is_100_eta_0():
    checks = [{"name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS", "started_at": None}]
    r = compute_progress(checks, ["ci"], _lookup({"ci": Expected(50, 100, 150, 8)}), NOW)
    assert r == ProgressResult(percent=100, eta_seconds=0, overdue=False)


def test_queued_expected_uses_p50_plus_queue_delay():
    # No live checks; one expected name → queued → rem = p50 + queueDelay
    r = compute_progress([], ["ci"], _lookup({"ci": Expected(50, 100, 150, 8)}), NOW,
                         queue_delay=lambda n: 20.0)
    assert r.percent == 0 and r.eta_seconds == 120 and r.overdue is False


def test_running_under_p50_eta_is_remaining():
    started = datetime(2026, 6, 25, 11, 59, 0, tzinfo=timezone.utc).isoformat()  # 60s elapsed
    checks = [{"name": "ci", "status": "IN_PROGRESS", "conclusion": None, "started_at": started}]
    r = compute_progress(checks, ["ci"], _lookup({"ci": Expected(50, 100, 150, 8)}), NOW,
                         samples=lambda n: [])  # <5 samples → p50/p90 path
    assert r.eta_seconds == 40 and r.overdue is False        # 100 - 60

def test_overdue_with_10plus_samples_all_below_elapsed():
    started = datetime(2026, 6, 25, 11, 0, 0, tzinfo=timezone.utc).isoformat()  # 3600s elapsed
    checks = [{"name": "ci", "status": "IN_PROGRESS", "conclusion": None, "started_at": started}]
    r = compute_progress(checks, ["ci"], _lookup({"ci": Expected(50, 100, 150, 12)}), NOW,
                         samples=lambda n: [90, 95, 100, 105, 110, 92, 97, 102, 107, 112])
    assert r.overdue is True and r.eta_seconds is None
