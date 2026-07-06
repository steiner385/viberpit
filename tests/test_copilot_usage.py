from datetime import datetime, timezone
from ccswitch import copilot

NOW = datetime(2026, 7, 6, tzinfo=timezone.utc)

def test_maps_premium_requests_to_both_windows():
    u = copilot.usage_from_proxy(
        {"premium": {"used": 300, "limit": 1500, "resets_at": "2026-08-01T00:00:00+00:00"}}, NOW)
    assert u is not None
    assert u.session_pct == 20.0 and u.weekly_pct == 20.0
    assert u.session_resets_at == u.weekly_resets_at
    assert u.session_resets_at.isoformat() == "2026-08-01T00:00:00+00:00"
    assert u.extra_usage_enabled is False

def test_garbage_payload_returns_none():
    assert copilot.usage_from_proxy({}, NOW) is None
    assert copilot.usage_from_proxy({"premium": {"used": 1, "limit": 0}}, NOW) is None
    assert copilot.usage_from_proxy({"premium": {"used": 1}}, NOW) is None

def test_pct_clamped_to_100():
    u = copilot.usage_from_proxy({"premium": {"used": 5000, "limit": 1500}}, NOW)
    assert u is not None and u.session_pct == 100.0 and u.session_resets_at is None
