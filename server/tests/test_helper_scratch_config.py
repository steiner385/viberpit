# server/tests/test_helper_scratch_config.py
"""#1005 — one-shot helper spawns must never be able to reset the shared account config.

The production failure: rocky read dobby's ``.claude.json`` through a 600s sshfs attribute
cache, got a truncated body (dobby rewrites the file atomically, so inode AND size change),
and Claude Code's config loader "repaired" the unparseable file by writing a **default**
config back — through the mount, onto dobby's disk. ~1,100 shared-config resets/day from
2026-09-01, every one of them wiping project trust, ``projects`` history and onboarding flags
that dobby's live sessions then re-accumulated.

The mount fix (``deploy/bin-ccswitch-mounts.sh``, covered in ``test_mounts_unit.py``) removes
the cause. This file covers the containment: a helper spawn gets a **host-local copy**, so a
false "corrupted" verdict can only damage a directory this host owns.
"""

import json
import os
from pathlib import Path

import pytest

from ccswitch.configdir import CLAUDE_JSON_NAME, CREDENTIALS_NAME
from server.sessions import scratch_config


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A stand-in account store + a host-local scratch root, both under tmp_path."""
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path / "accounts"))
    monkeypatch.setenv("CCSWITCH_HELPER_SCRATCH_ROOT", str(tmp_path / "scratch"))
    monkeypatch.delenv("CCSWITCH_HELPER_SCRATCH_CONFIG", raising=False)
    cfg = tmp_path / "accounts" / "cfg" / "alpha"
    cfg.mkdir(parents=True)
    (cfg / CLAUDE_JSON_NAME).write_text(json.dumps({
        "oauthAccount": {"emailAddress": "a@example.com"},
        "hasCompletedOnboarding": True,
        "projects": {"/work/repo": {"hasTrustDialogAccepted": True}},
    }))
    (cfg / CREDENTIALS_NAME).write_text(json.dumps({"claudeAiOauth": {"accessToken": "tok"}}))
    (cfg / "commands").mkdir()
    return cfg


def test_helper_gets_a_host_local_dir_not_the_shared_account_dir(store):
    got = Path(scratch_config.helper_config_dir(str(store)))
    assert got != store
    with pytest.raises(ValueError):
        got.relative_to(store.parent)  # not anywhere inside the account store


def test_config_files_are_copies_that_carry_the_account_identity(store):
    got = Path(scratch_config.helper_config_dir(str(store)))
    for name in (CLAUDE_JSON_NAME, CREDENTIALS_NAME):
        assert not (got / name).is_symlink(), f"{name} must be a copy — a symlink writes back"
        assert json.loads((got / name).read_text()) == json.loads((store / name).read_text())
    # 0600: the copy holds a plaintext OAuth token (constitution IV).
    assert os.stat(got / CREDENTIALS_NAME).st_mode & 0o777 == 0o600
    assert os.stat(got).st_mode & 0o777 == 0o700


def test_a_repair_write_in_the_scratch_dir_cannot_reach_the_shared_config(store):
    """THE regression. Simulate exactly what Claude Code did to dobby's file."""
    original = (store / CLAUDE_JSON_NAME).read_text()
    got = Path(scratch_config.helper_config_dir(str(store)))

    (got / CLAUDE_JSON_NAME).write_text(json.dumps({"hasCompletedOnboarding": False}))

    assert (store / CLAUDE_JSON_NAME).read_text() == original, \
        "a default config written by a helper must not propagate to the account store"


def test_shared_assets_are_symlinked_not_duplicated_per_host(store):
    """plugins/commands/settings are read-only shared state — copying them forks the truth."""
    got = Path(scratch_config.helper_config_dir(str(store)))
    assert (got / "commands").is_symlink()
    assert Path(os.readlink(got / "commands")) == store / "commands"


def test_a_torn_read_reuses_the_last_known_good_copy_and_leaves_evidence(store):
    """The truncated-body case: serve stale-but-valid config, and make the event countable."""
    good = Path(scratch_config.helper_config_dir(str(store)))
    assert scratch_config.recent_torn_reads() == 0

    # A cached-size read of an atomically-replaced file: the body is cut mid-string.
    (store / CLAUDE_JSON_NAME).write_text('{"oauthAccount": {"emailAddr')

    again = Path(scratch_config.helper_config_dir(str(store)))
    assert again == good
    assert json.loads((again / CLAUDE_JSON_NAME).read_text())["hasCompletedOnboarding"] is True
    assert scratch_config.recent_torn_reads() == 1, "a torn read must be recorded, not swallowed"
    assert scratch_config.recent_torn_reads(window_s=0.0) == 0, "the window must be honoured"


def test_a_torn_read_with_no_previous_copy_degrades_to_the_account_dir(store):
    """No last-known-good to serve: behave exactly as before this change, not worse."""
    (store / CLAUDE_JSON_NAME).write_text('{"oauthAccount": {"emailAddr')
    assert scratch_config.helper_config_dir(str(store)) == str(store)


def test_the_copy_refreshes_when_the_account_config_legitimately_changes(store):
    """Isolation must not mean staleness — a real edit on dobby has to reach helpers."""
    first = Path(scratch_config.helper_config_dir(str(store)))
    (store / CLAUDE_JSON_NAME).write_text(json.dumps({"hasCompletedOnboarding": True,
                                                      "marker": "fresh"}))
    second = Path(scratch_config.helper_config_dir(str(store)))
    assert json.loads((second / CLAUDE_JSON_NAME).read_text())["marker"] == "fresh"
    assert second == first


def test_two_accounts_never_share_a_scratch_dir(store, tmp_path):
    beta = store.parent / "beta"
    beta.mkdir()
    (beta / CLAUDE_JSON_NAME).write_text(json.dumps({"oauthAccount": {"emailAddress": "b@x"}}))
    a = scratch_config.helper_config_dir(str(store))
    b = scratch_config.helper_config_dir(str(beta))
    assert a != b
    assert json.loads(Path(b, CLAUDE_JSON_NAME).read_text())["oauthAccount"]["emailAddress"] == "b@x"


def test_kill_switch_and_fail_open_paths_return_the_input_unchanged(store, monkeypatch):
    monkeypatch.setenv("CCSWITCH_HELPER_SCRATCH_CONFIG", "false")
    assert scratch_config.helper_config_dir(str(store)) == str(store)
    monkeypatch.setenv("CCSWITCH_HELPER_SCRATCH_CONFIG", "true")
    assert scratch_config.helper_config_dir("") == ""
    assert scratch_config.helper_config_dir("/nope/not/a/dir") == "/nope/not/a/dir"


def test_a_scratch_root_inside_the_account_store_is_refused(store, monkeypatch):
    """Isolating into the very store we are isolating from would be a silent no-op."""
    monkeypatch.setenv("CCSWITCH_HELPER_SCRATCH_ROOT", str(store.parent / "scratch"))
    assert scratch_config.helper_config_dir(str(store)) == str(store)


def test_torn_read_markers_are_bounded(store, monkeypatch):
    """An unfixed mount produces these continuously — they must not fill the disk."""
    monkeypatch.setattr(scratch_config, "_MAX_TORN_READ_MARKERS", 3)
    scratch_config.helper_config_dir(str(store))  # seed a last-known-good copy
    (store / CLAUDE_JSON_NAME).write_text('{"oauthAccount": {"emailAddr')
    for _ in range(10):
        scratch_config.helper_config_dir(str(store))
    markers = list((scratch_config.scratch_root() / "torn-reads").iterdir())
    assert len(markers) <= 3


# --- the funnel: account_auth.helper_env is what every one-shot call site goes through ---


def test_helper_env_exports_the_scratch_dir(store):
    from server.sessions.account_auth import helper_env
    env = helper_env(str(store))
    assert env["CLAUDE_CONFIG_DIR"] != str(store)
    assert Path(env["CLAUDE_CONFIG_DIR"], CLAUDE_JSON_NAME).exists()


def test_helper_env_writes_back_opt_out_keeps_the_real_account_dir(store):
    """`claude mcp login` stores its OAuth token in the cfg dir — scratch would strand it."""
    from server.sessions.account_auth import helper_env
    assert helper_env(str(store), writes_back=True)["CLAUDE_CONFIG_DIR"] == str(store)


def test_mcp_login_is_the_only_write_back_call_site():
    """Guard the opt-out: a stateless helper that copies it re-opens #1005."""
    import subprocess
    root = Path(__file__).resolve().parents[2]
    out = subprocess.run(
        ["grep", "-rn", r"helper_env(.*writes_back=True", "server/", "src/"],
        cwd=root, capture_output=True, text=True,
    ).stdout
    sites = {
        line.split(":", 1)[0] for line in out.splitlines()
        if line.strip() and not line.startswith("server/tests/")
    }
    assert sites == {"server/mcp_auth.py"}, f"unexpected write-back helper spawn(s): {sites}"


def test_live_sessions_still_launch_against_the_real_account_dir():
    """Only stateless helpers are isolated: a session's history and token refreshes belong
    in the account dir, and routing it to scratch would silently fork every session's state."""
    src = (Path(__file__).resolve().parents[1] / "sessions" / "claude_sdk_factory.py").read_text()
    body = src.split("def real_sdk_factory", 1)[1].split("\ndef ", 1)[0]
    assert "CLAUDE_CONFIG_DIR=config_dir" in body
    assert "helper_config_dir" not in body and "helper_env" not in body
