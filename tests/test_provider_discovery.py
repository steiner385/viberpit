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


def test_account_names_dedupes_across_providers(tmp_path, monkeypatch):
    """Regression: _account_names() must return a sorted UNION (deduplicated names) across providers."""
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path))  # paths.root() honors this
    # Create both an anthropic slot and a copilot token-dir with the SAME name
    _mk_anthropic(tmp_path, "dup")
    _mk_copilot("dup")
    # Verify all_accounts() yields the name twice (once per provider)
    assert accounts.all_accounts() == [("dup", "anthropic"), ("dup", "copilot")]
    # Verify _account_names() dedupes: "dup" appears exactly once
    names = accounts._account_names()
    assert names.count("dup") == 1
    assert names == ["dup"]
