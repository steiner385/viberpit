"""The VIBERPIT_* <-> CCSWITCH_* env mirror (rebrand Phase 0b).

The mirror is what lets the ~211 legacy env names be renamed later without touching
a single /etc env file, so its rules are load-bearing: fill both ways, never
overwrite what the process was launched with, and cover dynamically-built names.
"""
import os

import pytest

from viberpit import brand, env_compat
from viberpit.env_compat import mirror_legacy_env


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
    env = {"HOME": "/home/tony", "CCSWITCH_": "x", "MYVIBERPIT_A": "y"}
    assert mirror_legacy_env(env) == {}
    assert env == {"HOME": "/home/tony", "CCSWITCH_": "x", "MYVIBERPIT_A": "y"}


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


def test_withheld_env_empties_the_namespace_in_both_spellings():
    """Every agent CLI's env override (#3386). Live re-run 2026-11-13 (#3367): a shell
    listed the legacy-prefix master key — the daemon's env, and env_compat's legacy
    mirror of it, rode the CLI's env into the agent's shell."""
    env = {"PATH": "/usr/bin", "HOME": "/home/u",
           f"{brand.ENV_PREFIX}MASTER_KEY": "k", f"{brand.LEGACY_ENV_PREFIX}MASTER_KEY": "k",
           f"{brand.ENV_PREFIX}DB_URL": "postgresql://u:pw@h/db"}
    assert env_compat.withheld_env(env) == {f"{brand.ENV_PREFIX}MASTER_KEY": "",
                                            f"{brand.LEGACY_ENV_PREFIX}MASTER_KEY": "",
                                            f"{brand.ENV_PREFIX}DB_URL": ""}


# ── what a session's own tooling still needs (#3386 follow-up) ──────────────

#: Names a session's ``viberpit``/``ccrun``/``ccmax`` and the core tests read, which the
#: withheld env keeps (``env_compat.WITHHELD_ENV_KEEP``).
_KEPT = {"DAEMON_ROLE": "worker", "CLAUDE_BIN": "/opt/claude --x",
         "HOME": "/srv/accounts", "REPO_ROOT": "/srv/repos"}


def _both(suffix_values):
    return {f"{p}{s}": v for p in (brand.ENV_PREFIX, brand.LEGACY_ENV_PREFIX)
            for s, v in suffix_values.items()}


def test_withheld_env_keeps_what_a_sessions_own_tooling_reads():
    env = {**_both(_KEPT), **_both({"MASTER_KEY": "k"})}
    assert env_compat.withheld_env(env) == _both({"MASTER_KEY": ""})


def test_a_kept_name_is_still_withheld_when_its_value_carries_userinfo():
    env = _both({s: "https://user:pw@host/x" for s in env_compat.WITHHELD_ENV_KEEP})
    assert env_compat.withheld_env(env) == {k: "" for k in env}


def test_no_kept_name_is_secret_shaped():
    for suffix in env_compat.WITHHELD_ENV_KEEP:
        assert not env_compat.is_secret_shaped(f"{brand.ENV_PREFIX}{suffix}", "")


def test_secret_shaped_names_and_values():
    for name in ("VIBERPIT_MASTER_KEY", "VIBERPIT_GH_PROJECT_TOKEN", "VIBERPIT_SESSION_SECRET",
                 "VIBERPIT_PG_PASSWORD", "VIBERPIT_DB_URL", "VIBERPIT_SENTRY_DSN",
                 "VIBERPIT_SESSIOND_TLS_KEY", "VIBERPIT_CLAUDE_CREDS_DIR",
                 "VIBERPIT_OAT_WORK_GMAIL_COM", f"{brand.LEGACY_ENV_PREFIX}GITHUB_APP_PRIVATE_KEY_PATH"):
        assert env_compat.is_secret_shaped(name, ""), name
    assert env_compat.is_secret_shaped("VIBERPIT_REPO_ROOT", "postgresql://u:pw@h/db")
    assert not env_compat.is_secret_shaped("VIBERPIT_REPO_ROOT", "/srv/repos")


@pytest.fixture
def session_shell(monkeypatch):
    """os.environ as a session's shell sees it: a worker daemon's env, then the override."""
    for name, value in {**_both(_KEPT), **_both({"MASTER_KEY": "k"})}.items():
        monkeypatch.setenv(name, value)
    for name, value in env_compat.withheld_env(dict(os.environ)).items():
        monkeypatch.setenv(name, value)


def test_a_worker_session_still_knows_it_is_on_a_worker(session_shell):
    """rocky's daemon runs with DAEMON_ROLE=worker; ``daemon_role()`` raises on ""."""
    from viberpit import fleet_role
    assert fleet_role.daemon_role() == "worker"


def test_ccmax_still_finds_the_claude_command(session_shell):
    from viberpit import launcher
    from viberpit.config import load_config
    assert launcher._claude_cmd(load_config()) == ["/opt/claude", "--x"]


def test_viberpit_still_reads_the_account_store_and_repo_root(session_shell):
    from viberpit import cli, paths
    assert str(paths.root()) == "/srv/accounts"
    assert cli._repo_root() == "/srv/repos"
