"""The VIBERPIT_* <-> CCSWITCH_* env mirror (rebrand Phase 0b).

The mirror is what lets the ~211 legacy env names be renamed later without touching
a single /etc env file, so its rules are load-bearing: fill both ways, never
overwrite what the process was launched with, and cover dynamically-built names.
"""
import os

from ccswitch import brand, env_compat
from ccswitch.env_compat import mirror_legacy_env


def test_prefixes_come_from_brand():
    assert env_compat.LEGACY_PREFIX == "CCSWITCH_"
    assert env_compat.NEW_PREFIX == "VIBERPIT_"
    assert (env_compat.NEW_PREFIX, env_compat.LEGACY_PREFIX) == (
        brand.ENV_PREFIX, brand.LEGACY_ENV_PREFIX)


def test_legacy_fills_new():
    env = {"CCSWITCH_DB_URL": "postgresql://x", "PATH": "/usr/bin"}
    filled = mirror_legacy_env(env)
    assert env["VIBERPIT_DB_URL"] == "postgresql://x"
    assert filled == {"VIBERPIT_DB_URL": "CCSWITCH_DB_URL"}


def test_new_fills_legacy():
    """A host whose env file has already migrated keeps feeding legacy readers."""
    env = {"VIBERPIT_MASTER_KEY": "k"}
    filled = mirror_legacy_env(env)
    assert env["CCSWITCH_MASTER_KEY"] == "k"
    assert filled == {"CCSWITCH_MASTER_KEY": "VIBERPIT_MASTER_KEY"}


def test_both_set_and_disagreeing_keeps_the_launched_values():
    """Neither direction may overwrite: the process keeps what it was launched with."""
    env = {"CCSWITCH_HOST": "127.0.0.1", "VIBERPIT_HOST": "0.0.0.0"}
    assert mirror_legacy_env(env) == {}
    assert env == {"CCSWITCH_HOST": "127.0.0.1", "VIBERPIT_HOST": "0.0.0.0"}


def test_empty_value_is_a_set_value():
    """"Set but empty" is a real tri-state in this codebase; it must not be refilled."""
    env = {"CCSWITCH_TAILNET_ALLOWLIST": "", "VIBERPIT_TAILNET_ALLOWLIST": "someone@"}
    mirror_legacy_env(env)
    assert env["CCSWITCH_TAILNET_ALLOWLIST"] == ""


def test_dynamic_oat_names_are_covered():
    """CCSWITCH_OAT_<ACCOUNT> is built at runtime (crypto.py), so the sweep is over
    os.environ rather than a fixed list of known settings."""
    env = {"CCSWITCH_OAT_WORK_GMAIL_COM": "sk-legacy", "VIBERPIT_OAT_PERSONAL": "sk-new"}
    mirror_legacy_env(env)
    assert env["VIBERPIT_OAT_WORK_GMAIL_COM"] == "sk-legacy"
    assert env["CCSWITCH_OAT_PERSONAL"] == "sk-new"


def test_unrelated_and_bare_prefix_names_are_untouched():
    env = {"HOME": "/home/tony", "CCSWITCH_": "x", "MYCCSWITCH_A": "y"}
    assert mirror_legacy_env(env) == {}
    assert env == {"HOME": "/home/tony", "CCSWITCH_": "x", "MYCCSWITCH_A": "y"}


def test_is_idempotent_on_a_mapping():
    env = {"CCSWITCH_PORT": "8787"}
    mirror_legacy_env(env)
    assert mirror_legacy_env(env) == {}
    assert env == {"CCSWITCH_PORT": "8787", "VIBERPIT_PORT": "8787"}


def test_os_environ_is_mirrored_once_per_process(monkeypatch):
    """The mirror is a launch-time snapshot, not a live sync: re-running it must not
    resurrect a name that was deliberately unset after start (every monkeypatched
    test in the suite depends on that)."""
    fake = {"CCSWITCH_ENV_COMPAT_PROBE": "1"}
    monkeypatch.setattr(os, "environ", fake)
    monkeypatch.setattr(env_compat, "_mirrored", False)

    assert mirror_legacy_env() == {"VIBERPIT_ENV_COMPAT_PROBE": "CCSWITCH_ENV_COMPAT_PROBE"}
    del fake["CCSWITCH_ENV_COMPAT_PROBE"]

    assert mirror_legacy_env() == {}  # latched: the second call does nothing
    assert "CCSWITCH_ENV_COMPAT_PROBE" not in fake


def test_force_re_runs_against_os_environ(monkeypatch):
    fake = {"VIBERPIT_ENV_COMPAT_PROBE": "2"}
    monkeypatch.setattr(os, "environ", fake)
    monkeypatch.setattr(env_compat, "_mirrored", True)

    assert mirror_legacy_env() == {}
    assert mirror_legacy_env(force=True) == {
        "CCSWITCH_ENV_COMPAT_PROBE": "VIBERPIT_ENV_COMPAT_PROBE"}
    assert fake["CCSWITCH_ENV_COMPAT_PROBE"] == "2"
