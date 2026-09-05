"""The account store is on the sshfs mount that produced torn reads (#1005).

A transient ``EIO``/``ESTALE``/``PermissionError`` there must degrade a read, not
abort a spawn or a rotation. The predecessor of ``provider_of`` was
``paths.copilot_token_dir(name).exists()``, and ``Path.exists()`` swallows
``OSError`` and answers False; the manifest's ``read_text`` and the discovery
``iterdir`` do not. These tests use a real unreadable file/dir (``chmod``) rather
than a monkeypatched raise wherever the fault is reachable that way, so they
exercise the actual errno path.

They also pin the two write-side invariants a fault must not break: an unreadable
or corrupt manifest is never published over (``os.replace`` would turn a transient
fault into permanent identity loss), and ``remove_account`` always clears the
ledger row that the health watchdog pages on.
"""
import json
import os
import stat
from pathlib import Path

import pytest

from ccswitch import account_registry, accounts, ledger, paths
from tests.helpers import register_accounts, temp_env

pytestmark = pytest.mark.skipif(
    os.geteuid() == 0, reason="root ignores mode bits, so chmod cannot fake a fault")


def _seed_slot(name):
    paths.store_dir().mkdir(parents=True, exist_ok=True)
    paths.slot_path(name).write_text(json.dumps({"claudeAiOauth": {}}))


def _seed_copilot(name):
    d = paths.copilot_token_dir(name)
    d.mkdir(parents=True, exist_ok=True)
    (d / "credentials.json").write_text("{}")


def _chmod(p: Path, mode: int):
    os.chmod(p, mode)


def _restore(p: Path):
    os.chmod(p, stat.S_IRWXU)


# --- reads degrade ----------------------------------------------------------


def test_provider_or_none_degrades_when_the_manifest_is_unreadable():
    """The hot path: ``configdir.compose`` runs this on EVERY spawn and reassign,
    and the orchestrator on every rotation. ``read_text`` raises where the old
    ``Path.exists()`` probe swallowed the same errno."""
    with temp_env():
        register_accounts("work")
        manifest = paths.accounts_manifest_path()
        _chmod(manifest, 0o000)
        try:
            assert accounts.provider_or_none("work") is None
        finally:
            _restore(manifest)


def test_provider_of_degrades_to_unknown_when_the_manifest_is_unreadable():
    """The strict door still raises — but a registry error, not a raw OSError, so
    every caller that already handles ``UnknownAccountError`` keeps working."""
    with temp_env():
        register_accounts("work")
        manifest = paths.accounts_manifest_path()
        _chmod(manifest, 0o000)
        try:
            with pytest.raises(account_registry.UnknownAccountError):
                accounts.provider_of("work")
        finally:
            _restore(manifest)


def test_provider_or_none_degrades_when_the_copilot_store_is_unreadable():
    """``discover_filesystem_accounts`` iterdirs the store on EVERY read (via
    ``ensure_manifest``), so one unreadable provider directory used to abort the
    whole lookup — and with it the spawn."""
    with temp_env():
        _seed_slot("work")
        _seed_copilot("cp")
        cp_store = paths.copilot_store_dir()
        _chmod(cp_store, 0o111)   # traversable, not readable -> iterdir raises
        try:
            # The anthropic half still enumerated, so the account still resolves.
            assert accounts.provider_or_none("work") == "anthropic"
        finally:
            _restore(cp_store)


def test_discovery_guards_each_provider_independently():
    """One provider's scan failing must cost only that provider's enumeration."""
    with temp_env():
        _seed_slot("work")
        _seed_copilot("cp")
        cp_store = paths.copilot_store_dir()
        _chmod(cp_store, 0o111)
        try:
            found = account_registry.discover_filesystem_accounts()
        finally:
            _restore(cp_store)
        assert found == [("work", account_registry.PROVIDER_ANTHROPIC)]


def test_provider_or_none_degrades_on_any_registry_fault(monkeypatch):
    """Belt-and-braces for faults the lower layers do not cover (a write-side
    ``AccountRegistryError`` from reconcile-on-read, an ``EIO`` from the lock
    file). ``provider_or_none`` is the degrade-gracefully door; ``provider_of``
    stays strict for callers that want the error."""
    with temp_env():
        register_accounts("work")

        def _boom():
            raise OSError(5, "Input/output error")

        monkeypatch.setattr(account_registry, "ensure_manifest", _boom)
        assert accounts.provider_or_none("work") is None
        with pytest.raises(OSError):
            accounts.provider_of("work")


def test_ensure_manifest_does_not_raise_on_an_unreadable_manifest():
    with temp_env():
        register_accounts("work")
        manifest = paths.accounts_manifest_path()
        _chmod(manifest, 0o000)
        try:
            account_registry.ensure_manifest()  # must not raise
        finally:
            _restore(manifest)


# --- writes refuse to destroy ----------------------------------------------


def test_locked_manifest_refuses_to_publish_over_a_corrupt_file():
    """``load_manifest`` promises it is "NOT overwriting" an unparseable file;
    ``locked_manifest``'s ``os.replace`` used to break that promise on the very
    next write."""
    with temp_env():
        register_accounts("work")
        path = paths.accounts_manifest_path()
        good = path.read_text()
        path.write_text("{not json at all")
        with pytest.raises(account_registry.AccountRegistryError):
            with account_registry.locked_manifest() as m:
                m.accounts.pop("work", None)
        assert path.read_text() == "{not json at all"
        # The refusal is only about corruption: repair it, and writes resume.
        path.write_text(good)
        account_registry.forget_account("work")
        assert "work" not in json.loads(path.read_text())["accounts"]


def test_ensure_manifest_never_publishes_over_a_corrupt_file():
    """Reconcile-on-read runs on every read; on a corrupt file it must stay in
    memory, or one sshfs torn read becomes permanent identity loss."""
    with temp_env():
        register_accounts("work", "other")
        path = paths.accounts_manifest_path()
        _seed_slot("ondisk")
        path.write_text("{not json at all")
        m = account_registry.ensure_manifest()
        assert "ondisk" in m.accounts                   # reconciled in memory
        assert path.read_text() == "{not json at all"   # but never published


# --- remove_account ---------------------------------------------------------


def test_remove_account_clears_the_ledger_when_the_manifest_write_fails(monkeypatch):
    """The credential slot and copilot dir are already deleted by the time the
    manifest is touched, so a fault there must not strand the ledger row: the
    health watchdog alerts on ledger status and would page needs-reauth forever
    (2026-07-21, long-deleted primary/secondary paged reauth all day)."""
    with temp_env():
        register_accounts("work")
        _seed_slot("work")
        with ledger.locked_ledger() as led:
            ledger.set_status(led, "work", "needs-reauth")

        def _boom(_name):
            raise OSError(5, "Input/output error")

        monkeypatch.setattr(account_registry, "forget_account", _boom)
        accounts.remove_account("work")  # must not propagate

        with ledger.locked_ledger() as led:
            assert "work" not in led["accounts"]
        assert not paths.slot_path("work").exists()


def test_remove_account_clears_the_ledger_when_assert_no_secrets_raises(monkeypatch):
    """The same abort window, reached by a non-OSError registry fault."""
    with temp_env():
        register_accounts("work")
        with ledger.locked_ledger() as led:
            ledger.set_status(led, "work", "needs-reauth")

        def _boom(_payload):
            raise account_registry.AccountRegistryError("refusing key")

        monkeypatch.setattr(account_registry, "assert_no_secrets", _boom)
        accounts.remove_account("work")

        with ledger.locked_ledger() as led:
            assert "work" not in led["accounts"]


def test_remove_account_still_drops_the_manifest_entry_on_the_happy_path():
    with temp_env():
        register_accounts("work", "keep")
        accounts.remove_account("work")
        assert {n for n, _ in accounts.all_accounts()} == {"keep"}


def test_accounts_import_reports_a_corrupt_manifest_instead_of_crashing(capsys):
    """``ccswitch accounts import`` is the documented repair command, so a corrupt
    file must produce its actionable message and exit 2 — not a traceback, and
    (per the fix above) not a silent overwrite."""
    from ccswitch import cli
    with temp_env():
        register_accounts("work")
        path = paths.accounts_manifest_path()
        path.write_text("{not json at all")
        assert cli.main(["accounts", "import"]) == 2
        assert "refusing to write" in capsys.readouterr().err
        assert path.read_text() == "{not json at all"
