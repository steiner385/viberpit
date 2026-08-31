"""Regressions for the "phantom empty account" wedge.

A stray `.cred.json` in the account store (a credential slot written for an EMPTY
account name) enumerated as a real account named ``""``. The usage-aware rotation
picker then handed that name to the orchestrator, which asked
``provider_of("")`` what kind of account it was — and got ``"copilot"``, because
``copilot_token_dir("")`` is ``copilot_store_dir() / ""``, i.e. the store ROOT,
which always exists once any Copilot account is configured. The orchestrator then
tried to start a Copilot proxy for a nameless account, that failed, and the live
session was left with no driver at all.

Two independent defects, both covered here:

1. Enumeration must never emit a name that *aliases the store root* (``""``,
   ``"."``, ``".."``). ``pathlib.Path.glob`` — unlike a shell glob — does not hide
   dotfiles, so ``*.cred.json`` happily matches ``.cred.json``.
2. ``provider_of`` must never classify such a name as a Copilot account, however
   it reached the function.
"""
import json

from ccswitch import accounts, paths


def _mk_anthropic(name):
    paths.store_dir().mkdir(parents=True, exist_ok=True)
    paths.slot_path(name).write_text(json.dumps({"claudeAiOauth": {}}))


def _mk_copilot(name):
    d = paths.copilot_token_dir(name)
    d.mkdir(parents=True, exist_ok=True)
    (d / "credentials.json").write_text("{}")


def test_stray_dot_cred_json_is_not_an_account(tmp_path, monkeypatch):
    """A bare `.cred.json` must not enumerate as an account named ''."""
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path))
    _mk_anthropic("work")
    # The exact artefact seen in production: a 2-byte `{}` written by a
    # sync_back() that was handed an empty account name.
    (paths.store_dir() / ".cred.json").write_text("{}")

    assert accounts._anthropic_account_names() == ["work"]
    assert accounts.all_accounts() == [("work", "anthropic")]
    assert "" not in accounts._account_names()


def test_dot_and_dotdot_slots_are_not_accounts(tmp_path, monkeypatch):
    """`..cred.json` / `...cred.json` alias the store root the same way."""
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path))
    _mk_anthropic("work")
    (paths.store_dir() / "..cred.json").write_text("{}")
    (paths.store_dir() / "...cred.json").write_text("{}")

    assert accounts._anthropic_account_names() == ["work"]


def test_provider_of_blank_name_is_not_copilot(tmp_path, monkeypatch):
    """provider_of('') must not resolve to the Copilot store ROOT."""
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path))
    _mk_copilot("copilot1")
    assert paths.copilot_store_dir().exists()

    assert accounts.provider_of("") == "anthropic"
    assert accounts.provider_of(".") == "anthropic"
    assert accounts.provider_of("..") == "anthropic"


def test_provider_of_still_detects_real_copilot_accounts(tmp_path, monkeypatch):
    """The guard must not blind provider_of to genuine Copilot accounts."""
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path))
    _mk_copilot("copilot1")
    _mk_anthropic("work")

    assert accounts.provider_of("copilot1") == "copilot"
    assert accounts.provider_of("work") == "anthropic"
