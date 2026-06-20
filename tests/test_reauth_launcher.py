import os, subprocess
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "scripts" / "ccswitch-reauth-browser"


def _env(tmp_path, prof, fake, **extra):
    # CCSWITCH_HOME pins the reauth-tmp root the launcher will accept, mirroring
    # ccswitch.paths.root(). Explicitly set it so a CCSWITCH_HOME leaking in from
    # the test runner's environment can't change the guard's decision.
    return dict(
        os.environ,
        CCSWITCH_HOME=str(tmp_path / "home"),
        CCSWITCH_REAUTH_PROFILE=str(prof),
        CCSWITCH_REAUTH_BROWSER_BIN=str(fake),
        HOME=str(tmp_path),
        **extra,
    )


def _fake_browser(tmp_path, body='echo "ARGS:$@"'):
    fake = tmp_path / "fakebrowser"
    fake.write_text(f"#!/usr/bin/env bash\n{body}\n")
    fake.chmod(0o755)
    return fake


def test_launcher_execs_browser_with_profile_and_url(tmp_path):
    fake = _fake_browser(tmp_path)
    prof = tmp_path / "home" / "reauth-tmp" / "reauth-x"
    prof.mkdir(parents=True)
    out = subprocess.run(
        [str(SCRIPT), "https://claude.ai/x"],
        env=_env(tmp_path, prof, fake),
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, out.stderr
    assert "--user-data-dir=" + str(prof) in out.stdout
    assert "https://claude.ai/x" in out.stdout


def test_launcher_accepts_real_reauth_tmp_dir(tmp_path):
    """Regression: the launcher's allowed root must match ccswitch.paths.reauth_tmp_dir().

    The launcher previously hardcoded ~/.config/ccswitch/reauth-tmp while
    paths.root() resolves to ~/.claude-accounts, so every real profile was
    refused (exit 2) and Chrome never opened. Pin CCSWITCH_HOME and confirm a
    profile under the path Python would actually create is accepted.
    """
    from ccswitch import paths

    home = tmp_path / "home"
    fake = _fake_browser(tmp_path)
    # what paths.reauth_tmp_dir() yields under this CCSWITCH_HOME
    os.environ_backup = os.environ.get("CCSWITCH_HOME")
    os.environ["CCSWITCH_HOME"] = str(home)
    try:
        prof = paths.reauth_tmp_dir() / "reauth-real"
    finally:
        if os.environ_backup is None:
            os.environ.pop("CCSWITCH_HOME", None)
        else:
            os.environ["CCSWITCH_HOME"] = os.environ_backup
    prof.mkdir(parents=True)
    assert str(prof).startswith(str(home / "reauth-tmp"))
    out = subprocess.run(
        [str(SCRIPT), "https://x"],
        env=_env(tmp_path, prof, fake),
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, out.stderr
    assert "--user-data-dir=" + str(prof) in out.stdout


def test_launcher_rejects_profile_outside_reauth_tmp(tmp_path):
    fake = _fake_browser(tmp_path, body="echo ran")
    bad = tmp_path / "evil"
    bad.mkdir()
    out = subprocess.run(
        [str(SCRIPT), "https://x"],
        env=_env(tmp_path, bad, fake),
        capture_output=True,
        text=True,
    )
    assert out.returncode != 0
    assert "ran" not in out.stdout


def test_script_is_executable():
    assert os.access(SCRIPT, os.X_OK)
