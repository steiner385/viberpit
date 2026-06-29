# server/tests/test_check_estimate.py
from datetime import datetime, timezone
from server.pipeline.check_eta import check_estimate, CheckEstimate
from server.pipeline.history import Expected

NOW = datetime(2026, 6, 29, 12, 10, 0, tzinfo=timezone.utc)
EXP = Expected(p10=120.0, p50=300.0, p90=600.0, n=20)


def _chk(status, started=None, conclusion=None):
    return {"name": "ci", "status": status, "started_at": started, "conclusion": conclusion}


def test_completed_zero_remaining():
    est = check_estimate(_chk("COMPLETED"), EXP, [], NOW, fallback=300.0)
    assert est.progress == 1.0
    assert (est.rem_low, est.rem_nominal, est.rem_high) == (0.0, 0.0, 0.0)
    assert est.unfinished is False


def test_skipped_returns_none():
    assert check_estimate(_chk("COMPLETED", conclusion="SKIPPED"), EXP, [], NOW, 300.0) is None


def test_running_uses_p10_p50_p90_minus_elapsed():
    # started 1 min ago → elapsed 60s; rem = p* - 60, floored at 0
    est = check_estimate(_chk("IN_PROGRESS", "2026-06-29T12:09:00Z"), EXP, [], NOW, fallback=300.0)
    assert est.rem_low == 120.0 - 60.0
    assert est.rem_nominal == 300.0 - 60.0
    assert est.rem_high == 600.0 - 60.0
    assert est.unfinished is True
    assert est.rem_low <= est.rem_nominal <= est.rem_high


def test_running_long_floors_low_at_zero():
    # started 3 min ago → elapsed 180 > p10(120) → rem_low floors to 0
    est = check_estimate(_chk("IN_PROGRESS", "2026-06-29T12:07:00Z"), EXP, [], NOW, fallback=300.0)
    assert est.rem_low == 0.0
    assert est.rem_nominal == 300.0 - 180.0


def test_running_with_qualifying_samples_uses_percentiles():
    # elapsed 60s; samples all > elapsed → percentile-based remaining
    samples = [100.0, 200.0, 300.0, 400.0, 500.0]  # >=5, all > 60
    est = check_estimate(_chk("IN_PROGRESS", "2026-06-29T12:09:00Z"), EXP, samples, NOW, 300.0)
    # nominal remaining = median(samples) - elapsed = 300 - 60
    assert est.rem_nominal == 300.0 - 60.0
    assert est.rem_low <= est.rem_nominal <= est.rem_high


def test_queued_adds_queue_delay():
    est = check_estimate(_chk("QUEUED"), EXP, [], NOW, fallback=300.0, queue_delay=30.0)
    assert est.rem_low == 120.0 + 30.0
    assert est.rem_nominal == 300.0 + 30.0
    assert est.rem_high == 600.0 + 30.0
    assert est.progress == 0.0


def test_no_expected_uses_fallback():
    est = check_estimate(_chk("IN_PROGRESS", "2026-06-29T12:09:00Z"), None, [], NOW, fallback=200.0)
    # p50=p10=p90=fallback when no Expected → rem_nominal = 200 - 60
    assert est.rem_nominal == 200.0 - 60.0
