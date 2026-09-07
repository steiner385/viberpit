"""Process-backed quota (Copilot, Codex) is served from the ledger, on its own TTL.

Anthropic usage is fetched cold over REST, so a 60s cache is cheap to keep
warm. Copilot and Codex quota can only be read through a process (the
copilot2api proxy, the ``codex app-server``) that ``server.usage.process_quota``
starts on demand and lets the idle reaper stop. The synchronous core never
starts one: it reads what that refresher last persisted, and treats it as fresh
for ``process_quota_ttl_sec`` — the refresher's cold cadence — rather than
the REST TTL, which would declare every cold reading stale 60s after it landed.
"""
from datetime import datetime, timedelta, timezone

from ccswitch import account_registry, accounts, ledger, paths
from ccswitch.config import load_config
from ccswitch.quota import copilot_snapshot
from tests.helpers import temp_env, usage

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)


def _seed_copilot(name):
    d = paths.copilot_token_dir(name)
    d.mkdir(parents=True, exist_ok=True)
    (d / "credentials.json").write_text("{}")


def _record(name, snapshot, fetched_at):
    with ledger.locked_ledger() as led:
        ledger.record_usage(led, name, snapshot, fetched_at)


def test_process_quota_ttl_is_longer_than_the_rest_cache_ttl():
    cfg = load_config()
    assert cfg.process_quota_ttl_sec > cfg.usage_cache_ttl_sec


def test_anthropic_cached_usage_still_expires_on_the_rest_ttl():
    with temp_env():
        cfg = load_config()
        paths.store_dir().mkdir(parents=True, exist_ok=True)
        paths.slot_path("work").write_text('{"claudeAiOauth": {}}')
        _record("work", usage(session=10.0), NOW - timedelta(seconds=cfg.usage_cache_ttl_sec + 1))
        with ledger.locked_ledger() as led:
            assert ledger.cached_usage(led, "work", cfg, NOW) is None


def test_copilot_cached_usage_lives_for_the_process_quota_ttl():
    with temp_env():
        cfg = load_config()
        _seed_copilot("cp")
        snap = copilot_snapshot(40.0, None, fetched_at=NOW)
        # Past the REST TTL but inside the process TTL: still fresh.
        _record("cp", snap, NOW - timedelta(seconds=cfg.usage_cache_ttl_sec + 1))
        with ledger.locked_ledger() as led:
            got = ledger.cached_usage(led, "cp", cfg, NOW)
        assert got is not None and got.windows == snap.windows
        # Past the process TTL: expired.
        _record("cp", snap, NOW - timedelta(seconds=cfg.process_quota_ttl_sec + 1))
        with ledger.locked_ledger() as led:
            assert ledger.cached_usage(led, "cp", cfg, NOW) is None


def test_openai_cached_usage_lives_for_the_process_quota_ttl():
    with temp_env():
        cfg = load_config()
        account_registry.register_account(
            "chatgpt", account_registry.PROVIDER_OPENAI, engine="codex")
        snap = copilot_snapshot(5.0, None, fetched_at=NOW)  # any snapshot shape will do
        _record("chatgpt", snap, NOW - timedelta(seconds=cfg.usage_cache_ttl_sec + 1))
        with ledger.locked_ledger() as led:
            assert ledger.cached_usage(led, "chatgpt", cfg, NOW) is not None


def test_usage_for_copilot_reads_the_ledger_and_never_touches_a_process():
    with temp_env():
        _seed_copilot("cp")
        snap = copilot_snapshot(50.0, None, fetched_at=NOW)
        _record("cp", snap, datetime.now(timezone.utc) - timedelta(minutes=2))
        u = accounts.usage_for("cp")
        assert u is not None and u.window("premium").used_pct == 50.0


def test_usage_for_copilot_is_none_past_the_process_ttl_and_never_backs_off():
    """No process reader lives in the core any more, so there is nothing to fail:
    an empty or expired ledger reads as "not read yet", not as a fetch failure
    that arms the shared cooldown against the refresher's next attempt."""
    with temp_env():
        cfg = load_config()
        _seed_copilot("cp")
        assert accounts.usage_for("cp") is None
        _record("cp", copilot_snapshot(50.0, None, fetched_at=NOW),
                datetime.now(timezone.utc) - timedelta(seconds=cfg.process_quota_ttl_sec + 1))
        assert accounts.usage_for("cp") is None
        with ledger.locked_ledger() as led:
            assert not ledger.usage_fetch_cooldown_active(led, "cp", 0)
            assert "usageFetchFails" not in led["accounts"].get("cp", {})


def test_core_has_no_copilot_usage_reader_seam():
    """The proxy read moved to server.usage.process_quota; a second reader in the
    core would be a parallel implementation of the same fetch."""
    assert not hasattr(accounts, "set_copilot_usage_reader")
    assert not hasattr(accounts, "_copilot_usage_for")
