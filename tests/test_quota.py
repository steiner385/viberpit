"""The QuotaSnapshot contract: window lists, binding window, availability."""
from datetime import datetime, timedelta, timezone

from ccswitch import quota
from ccswitch.quota import (AUTH_ERROR, AVAILABLE, QuotaSnapshot, QuotaWindow,
                            SCOPE_MODEL, UNKNOWN, anthropic_snapshot,
                            copilot_snapshot, unknown_snapshot)

NOW = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)


def test_anthropic_reports_two_windows_copilot_exactly_one():
    a = anthropic_snapshot(10.0, NOW, 20.0, NOW, fetched_at=NOW)
    assert [w.key for w in a.windows] == ["session", "weekly"]
    c = copilot_snapshot(20.0, NOW, fetched_at=NOW)
    assert [w.key for w in c.windows] == ["premium"]


def test_binding_window_is_the_least_headroom_one():
    u = anthropic_snapshot(10.0, NOW, 80.0, NOW, fetched_at=NOW)
    assert u.binding_window().key == "weekly"
    u = anthropic_snapshot(90.0, NOW, 20.0, NOW, fetched_at=NOW)
    assert u.binding_window().key == "session"


def test_binding_window_ties_resolve_to_the_earlier_window():
    """Reproduces the pair math exactly: weekly only bound when STRICTLY higher."""
    u = anthropic_snapshot(50.0, NOW, 50.0, NOW, fetched_at=NOW)
    assert u.binding_window().key == "session"


def test_model_scoped_window_only_binds_its_own_models():
    session = QuotaWindow(key="session", label="session", used_pct=5.0, resets_at=NOW)
    opus_only = QuotaWindow(key="opus_weekly", label="opus weekly", used_pct=95.0,
                            resets_at=NOW, scope=SCOPE_MODEL,
                            applies_to_models=("opus",))
    u = QuotaSnapshot(provider="anthropic", fetched_at=NOW,
                      windows=(session, opus_only))
    assert u.binding_window("sonnet").key == "session"
    assert u.binding_window("opus").key == "opus_weekly"
    # An UNKNOWN model matches every window — conservative, so a model-scoped
    # limit is never assumed inapplicable for a request that did not name a model.
    assert u.binding_window(None).key == "opus_weekly"


def test_unknown_and_auth_error_are_availability_not_a_fabricated_window():
    for availability in (UNKNOWN, AUTH_ERROR):
        u = unknown_snapshot("anthropic", fetched_at=NOW, availability=availability)
        assert u.windows == ()
        assert u.is_available is False
        assert u.binding_window() is None
        assert u.max_used_pct() is None


def test_reserve_window_is_the_longest_one():
    a = anthropic_snapshot(10.0, NOW, 20.0, NOW, fetched_at=NOW)
    assert a.reserve_window().key == "weekly"
    # A single-allowance provider measures the reserve on that single window.
    assert copilot_snapshot(20.0, NOW, fetched_at=NOW).reserve_window().key == "premium"


def test_null_reset_survives_a_roundtrip():
    """resets_at is None for an inactive window — a real state, not a missing one."""
    u = anthropic_snapshot(0.0, None, 0.0, None, fetched_at=NOW)
    assert all(w.resets_at is None for w in u.windows)
    back = QuotaSnapshot.from_dict(u.to_dict())
    assert all(w.resets_at is None for w in back.windows)


def test_roundtrip_preserves_every_field():
    u = QuotaSnapshot(
        provider="anthropic", fetched_at=NOW, availability=AVAILABLE,
        extra_usage_enabled=True,
        windows=(QuotaWindow(key="k", label="L", used_pct=33.5,
                             resets_at=NOW + timedelta(hours=2),
                             duration_seconds=3600, scope=SCOPE_MODEL,
                             applies_to_models=("opus", "sonnet")),))
    assert QuotaSnapshot.from_dict(u.to_dict()) == u


def test_serialised_blob_is_versioned():
    blob = anthropic_snapshot(1.0, NOW, 2.0, NOW, fetched_at=NOW).to_dict()
    assert blob["v"] == quota.QUOTA_BLOB_VERSION
