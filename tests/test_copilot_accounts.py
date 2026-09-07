from datetime import datetime, timezone
from ccswitch import accounts, paths


def _mk_copilot(name):
    d = paths.copilot_token_dir(name); d.mkdir(parents=True, exist_ok=True)
    (d / "credentials.json").write_text("{}")


def test_usage_for_copilot_serves_the_refresher_persisted_snapshot(tmp_path, monkeypatch):
    """The proxy read lives in server/usage/process_quota.py; the core reads
    the ledger it wrote (see tests/test_process_quota_ttl.py for the TTL)."""
    from ccswitch import copilot, ledger
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path))
    _mk_copilot("c1")
    now = datetime.now(timezone.utc)
    snap = copilot.usage_from_proxy(
        {"premium": {"used": 750, "limit": 1500, "resets_at": None}}, now)
    with ledger.locked_ledger() as led:
        ledger.record_usage(led, "c1", snap, now)
    u = accounts.usage_for("c1")
    assert u is not None and u.window("premium").used_pct == 50.0


def test_list_accounts_includes_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path))
    _mk_copilot("c1")
    entry = next(a for a in accounts.list_accounts() if a["name"] == "c1")
    assert entry["provider"] == "copilot"


def test_remove_account_clears_copilot_token_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path))
    _mk_copilot("c1")
    accounts.remove_account("c1")
    assert not paths.copilot_token_dir("c1").exists()
