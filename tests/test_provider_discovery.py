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
    # all_accounts() now reads the MANIFEST, which is keyed (and therefore
    # ordered) by name — not by the order the filesystem happened to be scanned.
    assert accounts.all_accounts() == [("copilot1", "copilot"), ("work", "anthropic")]


def test_colliding_name_resolves_to_one_account(tmp_path, monkeypatch):
    """One name is ONE account: names are globally unique in the manifest (v1).

    Discovery used to emit ``("dup", "anthropic")`` AND ``("dup", "copilot")`` for
    a name with both artefacts, while ``provider_of("dup")`` answered "copilot" —
    so the enumeration and the classification disagreed about the same account.
    The manifest resolves the collision once, keeping the classification the old
    ``provider_of`` gave (Copilot wins), so an existing dual-artefact install
    launches exactly as it did before.
    """
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path))  # paths.root() honors this
    _mk_anthropic(tmp_path, "dup")
    _mk_copilot("dup")

    assert accounts.all_accounts() == [("dup", "copilot")]
    assert accounts.provider_of("dup") == "copilot"
    names = accounts._account_names()
    assert names == ["dup"]
