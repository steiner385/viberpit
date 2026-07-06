from datetime import datetime, timezone
from ccswitch import accounts, paths


def _mk_copilot(name):
    d = paths.copilot_token_dir(name); d.mkdir(parents=True, exist_ok=True)
    (d / "credentials.json").write_text("{}")


def test_usage_for_copilot_uses_registered_reader(tmp_path, monkeypatch):
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path))
    _mk_copilot("c1")
    accounts.set_copilot_usage_reader(
        lambda name: {"premium": {"used": 750, "limit": 1500, "resets_at": None}})
    u = accounts.usage_for("c1")
    assert u is not None and u.session_pct == 50.0
    accounts.set_copilot_usage_reader(None)


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
