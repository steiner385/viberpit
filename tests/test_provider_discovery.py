import json
from ccswitch import accounts, paths, copilot

def _mk_anthropic(tmp, name):
    (paths.store_dir()).mkdir(parents=True, exist_ok=True)
    paths.slot_path(name).write_text(json.dumps({"claudeAiOauth": {}}))

def _mk_copilot(name):
    d = paths.copilot_token_dir(name); d.mkdir(parents=True, exist_ok=True)
    (d / "credentials.json").write_text("{}")

def test_union_discovery_and_provider_of(tmp_path, monkeypatch):
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path))  # paths.root() honors this
    _mk_anthropic(tmp_path, "work")
    _mk_copilot("copilot1")
    assert copilot.copilot_account_names() == ["copilot1"]
    assert set(accounts._account_names()) == {"work", "copilot1"}
    assert accounts.provider_of("work") == "anthropic"
    assert accounts.provider_of("copilot1") == "copilot"
    assert accounts.all_accounts() == [("work", "anthropic"), ("copilot1", "copilot")]
