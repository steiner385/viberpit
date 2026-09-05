from datetime import datetime, timezone

from ccswitch import copilot
from ccswitch.quota import WINDOW_PREMIUM

NOW = datetime(2026, 7, 6, tzinfo=timezone.utc)


def test_maps_premium_requests_to_exactly_one_window():
    """Copilot meters ONE allowance, so the snapshot has ONE window.

    The predecessor projected the premium-request percentage onto both a
    "session" and a "weekly" dimension because the old ``Usage`` dataclass had
    nowhere else to put it. That duplicate reached the accounts UI as two
    identical badges under different labels and made "which window binds?"
    meaningless for this provider. It must not come back.
    """
    u = copilot.usage_from_proxy(
        {"premium": {"used": 300, "limit": 1500, "resets_at": "2026-08-01T00:00:00+00:00"}}, NOW)
    assert u is not None
    assert [w.key for w in u.windows] == [WINDOW_PREMIUM]
    assert u.windows[0].used_pct == 20.0
    assert u.windows[0].resets_at.isoformat() == "2026-08-01T00:00:00+00:00"
    assert u.provider == "copilot"
    assert u.fetched_at == NOW
    assert u.extra_usage_enabled is False
    # No fabricated sibling under any of the Anthropic window keys.
    assert u.window("session") is None and u.window("weekly") is None


def test_single_window_is_the_binding_window():
    u = copilot.usage_from_proxy({"premium": {"used": 300, "limit": 1500}}, NOW)
    assert u.binding_window().key == WINDOW_PREMIUM
    # A reserve on a single-allowance provider measures that same window.
    assert u.reserve_window().key == WINDOW_PREMIUM


def test_garbage_payload_returns_none():
    assert copilot.usage_from_proxy({}, NOW) is None
    assert copilot.usage_from_proxy({"premium": {"used": 1, "limit": 0}}, NOW) is None
    assert copilot.usage_from_proxy({"premium": {"used": 1}}, NOW) is None


def test_pct_clamped_to_100():
    u = copilot.usage_from_proxy({"premium": {"used": 5000, "limit": 1500}}, NOW)
    assert u is not None
    assert u.windows[0].used_pct == 100.0
    assert u.windows[0].resets_at is None
