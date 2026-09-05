"""The accounts.json manifest: schema, idempotent import, and the no-secrets rule."""
import dataclasses
import json

import pytest

from ccswitch import account_registry, accounts, paths
from ccswitch.account_registry import (AccountEntry, ManifestNotWritable,
                                       UnknownAccountError)
from tests.helpers import temp_env


def _mk_anthropic(name):
    paths.store_dir().mkdir(parents=True, exist_ok=True)
    paths.slot_path(name).write_text(json.dumps({"claudeAiOauth": {}}))


def _mk_copilot(name):
    d = paths.copilot_token_dir(name)
    d.mkdir(parents=True, exist_ok=True)
    (d / "credentials.json").write_text("{}")


# --- schema ---------------------------------------------------------------


def test_manifest_lives_beside_the_ledger_not_inside_it():
    """Identity is a separate file from the cross-host multi-writer usage cache."""
    with temp_env():
        assert paths.accounts_manifest_path().name == "accounts.json"
        assert paths.accounts_manifest_path().parent == paths.ledger_path().parent
        assert paths.accounts_manifest_path() != paths.ledger_path()


def test_entry_carries_the_full_identity_record():
    with temp_env():
        _mk_anthropic("work")
        account_registry.import_filesystem_accounts()
        e = account_registry.entry("work")
        assert e.name == "work"
        assert e.provider == account_registry.PROVIDER_ANTHROPIC
        # engine is nullable: None means "the engine registry's default"
        # (engine_from_wire(None) -> claude_code), reusing that single branch
        # instead of copying the engine strings into src/ccswitch.
        assert e.engine is None
        assert e.user_auth_mode == account_registry.USER_AUTH_CLAUDE_OAUTH
        assert e.runner_auth_mode == account_registry.RUNNER_AUTH_CONFIG_DIR
        assert e.credential_authority == account_registry.AUTHORITY_LOCAL
        assert e.enabled is True
        assert e.generation == 1
        assert e.health.state == "unknown"
        assert e.created_at is not None and e.updated_at is not None


def test_copilot_entry_uses_the_copilot_auth_modes():
    with temp_env():
        _mk_copilot("cp")
        account_registry.import_filesystem_accounts()
        e = account_registry.entry("cp")
        assert e.provider == account_registry.PROVIDER_COPILOT
        assert e.user_auth_mode == account_registry.USER_AUTH_GITHUB_DEVICE
        assert e.runner_auth_mode == account_registry.RUNNER_AUTH_COPILOT_PROXY


def test_openai_entry_defers_to_the_codex_auth_authority():
    """An OpenAI account's refresh credential never lives on this host."""
    with temp_env():
        e = account_registry.entry_for("chatgpt1", account_registry.PROVIDER_OPENAI)
        assert e.user_auth_mode == account_registry.USER_AUTH_CHATGPT_DEVICE
        assert e.runner_auth_mode == account_registry.RUNNER_AUTH_CHATGPT_EXTERNAL_TOKENS
        assert e.credential_authority == account_registry.AUTHORITY_CODEX_AUTH


def test_fixed_contract_vocabularies_are_exactly_the_plan_values():
    assert account_registry.ACCOUNT_PROVIDERS == ("anthropic", "copilot", "openai")
    assert account_registry.USER_AUTH_MODES == (
        "claude_oauth", "github_device", "chatgpt_device")
    assert account_registry.RUNNER_AUTH_MODES == (
        "config_dir", "copilot_proxy", "chatgpt_external_tokens")


# --- no secrets, ever -----------------------------------------------------


def test_written_manifest_contains_no_credential_material():
    with temp_env():
        _mk_anthropic("work")
        _mk_copilot("cp")
        account_registry.import_filesystem_accounts()
        raw = paths.accounts_manifest_path().read_text()
        blob = raw.lower()
        for marker in ("token", "secret", "password", "refresh", "apikey", "cookie"):
            assert marker not in blob, f"accounts.json leaked a {marker!r} field"
        # …and the structural guard rejects one if a future field sneaks in.
        with pytest.raises(account_registry.AccountRegistryError):
            account_registry.assert_no_secrets(
                {"accounts": {"work": {"refreshToken": "sk-ant-oat01-x"}}})


def test_manifest_file_is_owner_only():
    with temp_env():
        _mk_anthropic("work")
        account_registry.import_filesystem_accounts()
        assert paths.accounts_manifest_path().stat().st_mode & 0o777 == 0o600


# --- idempotent importer --------------------------------------------------


def test_import_is_idempotent_and_does_not_bump_the_revision():
    with temp_env():
        _mk_anthropic("work")
        _mk_copilot("cp")
        first = account_registry.import_filesystem_accounts()
        assert set(first.added) == {"work", "cp"}
        body = paths.accounts_manifest_path().read_text()

        second = account_registry.import_filesystem_accounts()
        assert second.added == ()
        assert set(second.unchanged) == {"work", "cp"}
        assert second.revision == first.revision
        # Byte-identical: a re-scan writes nothing at all.
        assert paths.accounts_manifest_path().read_text() == body


def test_import_never_clobbers_operator_state():
    """Runtime/operator fields survive a re-import — the scan knows nothing of them."""
    with temp_env():
        _mk_anthropic("work")
        account_registry.import_filesystem_accounts()
        account_registry.set_health("work", "needs-reauth", detail="400 invalid_grant")
        gen = account_registry.bump_generation("work")
        with account_registry.locked_manifest() as m:
            m.accounts["work"] = dataclasses.replace(m.accounts["work"], enabled=False)

        account_registry.import_filesystem_accounts()

        e = account_registry.entry("work")
        assert e.enabled is False
        assert e.generation == gen
        assert e.health.state == "needs-reauth"
        assert e.health.detail == "400 invalid_grant"


def test_revision_is_monotonic_across_real_changes():
    with temp_env():
        _mk_anthropic("a")
        account_registry.import_filesystem_accounts()
        r1 = account_registry.load_manifest().revision
        _mk_anthropic("b")
        account_registry.import_filesystem_accounts()
        r2 = account_registry.load_manifest().revision
        assert r2 > r1
        assert account_registry.load_manifest().version == account_registry.MANIFEST_VERSION


# --- reads come from the manifest ----------------------------------------


def test_all_accounts_and_provider_of_read_the_manifest_not_the_filesystem():
    with temp_env():
        _mk_anthropic("work")
        account_registry.import_filesystem_accounts()
        # Declare an account the filesystem does NOT have; the manifest is truth.
        account_registry.register_account("ghost", account_registry.PROVIDER_OPENAI)
        assert ("ghost", "openai") in accounts.all_accounts()
        assert accounts.provider_of("ghost") == "openai"


def test_unknown_name_raises_instead_of_defaulting_to_anthropic():
    with temp_env():
        _mk_anthropic("work")
        account_registry.import_filesystem_accounts()
        with pytest.raises(UnknownAccountError):
            accounts.provider_of("nope")
        assert accounts.provider_or_none("nope") is None


def test_remove_account_drops_the_manifest_entry():
    with temp_env():
        _mk_anthropic("work")
        assert accounts.all_accounts() == [("work", "anthropic")]
        accounts.remove_account("work")
        assert accounts.all_accounts() == []
        with pytest.raises(UnknownAccountError):
            accounts.provider_of("work")


# --- health metadata is written by the real auth path ---------------------


def test_needs_reauth_flip_records_health_in_the_manifest(monkeypatch):
    """The health field is not decoration — the auth path writes it."""
    from ccswitch import oauth

    with temp_env():
        _mk_anthropic("work")
        assert account_registry.entry("work").health.state == "unknown"

        def boom(_name):
            raise oauth.AuthError("token refresh failed with 400", status=400,
                                  body='{"error":"invalid_grant"}')

        monkeypatch.setattr(accounts, "acquire_valid_token", boom)
        assert accounts.usage_for("work") is None

        health = account_registry.entry("work").health
        assert health.state == "needs-reauth"
        assert health.checked_at is not None
        assert health.detail == "refresh token rejected"
        # …and no credential material rode along with it.
        account_registry.assert_no_secrets(account_registry.load_manifest().to_dict())


def test_reauth_clears_health_and_advances_the_generation():
    with temp_env():
        _mk_anthropic("work")
        account_registry.set_health("work", "needs-reauth")
        before = account_registry.entry("work").generation

        accounts.mark_reauthenticated("work")

        e = account_registry.entry("work")
        assert e.health.state == "ok"
        assert e.generation == before + 1


def test_health_write_never_breaks_the_auth_path_on_a_runner(monkeypatch):
    from ccswitch import oauth

    with temp_env():
        _mk_anthropic("work")
        account_registry.import_filesystem_accounts()
        monkeypatch.setenv("CCSWITCH_DAEMON_ROLE", "worker")

        def boom(_name):
            raise oauth.AuthError("dead", status=400, body="{}")

        monkeypatch.setattr(accounts, "acquire_valid_token", boom)
        # The runner cannot write the manifest; the flip must still complete.
        assert accounts.usage_for("work") is None
        accounts.mark_reauthenticated("work")


# --- one writer -----------------------------------------------------------


def test_runner_host_cannot_write_the_manifest(monkeypatch):
    with temp_env():
        _mk_anthropic("work")
        account_registry.import_filesystem_accounts()
        monkeypatch.setenv("CCSWITCH_DAEMON_ROLE", "worker")
        assert account_registry.is_manifest_writer() is False
        with pytest.raises(ManifestNotWritable):
            account_registry.register_account("new", "anthropic")


def test_runner_reads_the_snapshot_and_reconciles_only_in_memory(monkeypatch):
    with temp_env():
        _mk_anthropic("work")
        account_registry.import_filesystem_accounts()
        before = paths.accounts_manifest_path().read_text()

        monkeypatch.setenv("CCSWITCH_DAEMON_ROLE", "worker")
        _mk_anthropic("appeared-late")
        # The read still SEES it (degrades gracefully) …
        assert accounts.provider_of("appeared-late") == "anthropic"
        # … but the runner wrote nothing.
        assert paths.accounts_manifest_path().read_text() == before


# --- resilience -----------------------------------------------------------


def test_corrupt_manifest_is_not_silently_overwritten():
    with temp_env():
        paths.ensure_root()
        paths.accounts_manifest_path().write_text("{not json")
        assert account_registry.load_manifest().accounts == {}
        assert paths.accounts_manifest_path().read_text() == "{not json"


def test_malformed_entry_is_dropped_not_fatal():
    with temp_env():
        paths.ensure_root()
        paths.accounts_manifest_path().write_text(json.dumps({
            "version": 1, "revision": 3,
            "accounts": {"good": {"provider": "anthropic"}, "bad": {"provider": None}},
        }))
        m = account_registry.load_manifest()
        assert "good" in m.accounts
        assert isinstance(m.accounts["good"], AccountEntry)
