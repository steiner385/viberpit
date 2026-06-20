import os, subprocess, stat
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "scripts" / "ccswitch-reauth-browser"


def test_launcher_execs_browser_with_profile_and_url(tmp_path):
    fake = tmp_path / "fakebrowser"
    fake.write_text("#!/usr/bin/env bash\necho \"ARGS:$@\"\n")
    fake.chmod(0o755)
    prof = tmp_path / ".config" / "ccswitch" / "reauth-tmp" / "reauth-x"
    prof.mkdir(parents=True)
    env = dict(
        os.environ,
        CCSWITCH_REAUTH_PROFILE=str(prof),
        CCSWITCH_REAUTH_BROWSER_BIN=str(fake),
        HOME=str(tmp_path),
    )
    out = subprocess.run(
        [str(SCRIPT), "https://claude.ai/x"], env=env, capture_output=True, text=True
    )
    assert out.returncode == 0
    assert "--user-data-dir=" + str(prof) in out.stdout
    assert "https://claude.ai/x" in out.stdout


def test_launcher_rejects_profile_outside_reauth_tmp(tmp_path):
    fake = tmp_path / "fakebrowser"
    fake.write_text("#!/usr/bin/env bash\necho ran\n")
    fake.chmod(0o755)
    bad = tmp_path / "evil"
    bad.mkdir()
    env = dict(
        os.environ,
        CCSWITCH_REAUTH_PROFILE=str(bad),
        CCSWITCH_REAUTH_BROWSER_BIN=str(fake),
        HOME=str(tmp_path),
    )
    out = subprocess.run(
        [str(SCRIPT), "https://x"], env=env, capture_output=True, text=True
    )
    assert out.returncode != 0
    assert "ran" not in out.stdout


def test_script_is_executable():
    assert os.access(SCRIPT, os.X_OK)
