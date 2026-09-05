"""A live ledger.json holds the PRE-VERSIONING usage blob. It must migrate on read.

Dobby and rocky both hold ledgers written by the old ``Usage`` encoding. Dropping
those entries would blank every account's usage for a full cache TTL right after a
deploy; misreading them would feed the selector garbage.
"""
import json
from datetime import datetime, timedelta, timezone

from ccswitch import account_registry, ledger, paths
from ccswitch.config import load_config
from ccswitch.quota import QUOTA_BLOB_VERSION
from tests.helpers import temp_env

NOW = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)


def _legacy_blob(session_pct, weekly_pct, *, session_reset=None, weekly_reset=None,
                 extra=False, fetched=NOW):
    """Exactly the shape ``ledger.record_usage`` wrote before this change."""
    return {
        "session_pct": session_pct,
        "session_resets_at": session_reset.isoformat() if session_reset else None,
        "weekly_pct": weekly_pct,
        "weekly_resets_at": weekly_reset.isoformat() if weekly_reset else None,
        "extra_usage_enabled": extra,
        "fetched_at": fetched.isoformat(),
    }


def _seed_anthropic(name):
    paths.store_dir().mkdir(parents=True, exist_ok=True)
    paths.slot_path(name).write_text(json.dumps({"claudeAiOauth": {}}))


def _seed_copilot(name):
    d = paths.copilot_token_dir(name)
    d.mkdir(parents=True, exist_ok=True)
    (d / "credentials.json").write_text("{}")


def test_new_blob_is_versioned():
    with temp_env():
        from tests.helpers import usage
        with ledger.locked_ledger() as led:
            ledger.record_usage(led, "a", usage(session=42.0), NOW)
            assert led["accounts"]["a"]["usage"]["v"] == QUOTA_BLOB_VERSION
            assert led["accounts"]["a"]["usage"]["fetched_at"] == NOW.isoformat()


def test_legacy_anthropic_entry_migrates_to_two_windows():
    with temp_env():
        _seed_anthropic("work")
        reset = NOW + timedelta(hours=3)
        with ledger.locked_ledger() as led:
            led["accounts"]["work"] = {
                "usage": _legacy_blob(11.0, 69.0, session_reset=reset, weekly_reset=reset)}
        with ledger.locked_ledger() as led:
            u = ledger.cached_usage(led, "work", load_config(), NOW)
        assert u is not None
        assert u.provider == "anthropic"
        assert [(w.key, w.used_pct) for w in u.windows] == [
            ("session", 11.0), ("weekly", 69.0)]
        assert u.window("session").resets_at == reset
        assert u.binding_window().key == "weekly"
        assert u.fetched_at == NOW


def test_legacy_extra_usage_flag_is_preserved():
    """The paid-tier decision reads this; losing it silently disables spending."""
    with temp_env():
        _seed_anthropic("work")
        with ledger.locked_ledger() as led:
            led["accounts"]["work"] = {"usage": _legacy_blob(100.0, 10.0, extra=True)}
        with ledger.locked_ledger() as led:
            u = ledger.cached_usage(led, "work", load_config(), NOW)
        assert u.extra_usage_enabled is True


def test_legacy_null_resets_survive_migration():
    with temp_env():
        _seed_anthropic("work")
        with ledger.locked_ledger() as led:
            led["accounts"]["work"] = {"usage": _legacy_blob(0.0, 0.0)}
        with ledger.locked_ledger() as led:
            u = ledger.cached_usage(led, "work", load_config(), NOW)
        assert all(w.resets_at is None for w in u.windows)
        assert u.binding_window().resets_at is None


def test_legacy_copilot_entry_collapses_the_fabricated_duplicate():
    """The old Copilot blob held the SAME number twice; migration restores one window."""
    with temp_env():
        _seed_copilot("cp")
        reset = NOW + timedelta(days=10)
        with ledger.locked_ledger() as led:
            led["accounts"]["cp"] = {
                "usage": _legacy_blob(20.0, 20.0, session_reset=reset, weekly_reset=reset)}
        with ledger.locked_ledger() as led:
            u = ledger.cached_usage(led, "cp", load_config(), NOW)
        assert u.provider == "copilot"
        assert [w.key for w in u.windows] == ["premium"]
        assert u.windows[0].used_pct == 20.0
        assert u.windows[0].resets_at == reset


def test_legacy_entry_still_honours_the_cache_ttl():
    with temp_env():
        _seed_anthropic("work")
        stale = NOW - timedelta(days=1)
        with ledger.locked_ledger() as led:
            led["accounts"]["work"] = {"usage": _legacy_blob(5.0, 5.0, fetched=stale)}
        with ledger.locked_ledger() as led:
            assert ledger.cached_usage(led, "work", load_config(), NOW) is None


def test_unreadable_blob_reads_as_unknown_not_as_a_crash():
    with temp_env():
        _seed_anthropic("work")
        with ledger.locked_ledger() as led:
            led["accounts"]["work"] = {"usage": {"fetched_at": NOW.isoformat(),
                                                 "session_pct": "not-a-number"}}
        with ledger.locked_ledger() as led:
            assert ledger.cached_usage(led, "work", load_config(), NOW) is None


def test_unregistered_account_with_legacy_blob_degrades_to_anthropic():
    """No manifest entry -> the historical default, rather than an exception in a
    read path every dashboard hit goes through."""
    with temp_env():
        with ledger.locked_ledger() as led:
            led["accounts"]["ghost"] = {"usage": _legacy_blob(3.0, 4.0)}
        with ledger.locked_ledger() as led:
            u = ledger.cached_usage(led, "ghost", load_config(), NOW)
        assert u.provider == "anthropic"
        assert len(u.windows) == 2
        assert not account_registry.has_account("ghost")
