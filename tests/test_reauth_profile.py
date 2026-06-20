import os
import sqlite3
import stat
from pathlib import Path

import pytest

from ccswitch import auth_login, paths


def _make_source(tmp_path):
    """Create a mock Chrome profile directory with a Cookies database."""
    src = tmp_path / "google-chrome" / "Default"
    src.mkdir(parents=True)
    (tmp_path / "google-chrome" / "Local State").write_text('{"os_crypt":{"encrypted_key":"x"}}')
    db = src / "Cookies"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE cookies(host_key TEXT, name TEXT)")
    c.executemany(
        "INSERT INTO cookies VALUES(?,?)",
        [
            (".google.com", "SID"),
            ("accounts.google.com", "X"),
            (".claude.ai", "sess"),
            (".claude.com", "sess2"),
            ("platform.claude.com", "p"),
            ("console.anthropic.com", "tok"),
        ],
    )
    c.commit()
    c.close()
    return str(src)


def test_prepare_strips_claude_keeps_google(tmp_path, monkeypatch):
    """Verify prepare_reauth_profile copies cookies, strips Claude/Anthropic rows, keeps Google."""
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path / "home"))
    src = _make_source(tmp_path)
    temp = auth_login.prepare_reauth_profile(src)
    assert auth_login._under_reauth_tmp(temp)
    # 0700 dir
    assert stat.S_IMODE(os.stat(temp).st_mode) == 0o700
    ck = Path(temp) / "Default" / "Cookies"
    rows = sqlite3.connect(ck).execute("SELECT host_key FROM cookies").fetchall()
    hosts = {r[0] for r in rows}
    assert ".google.com" in hosts and "accounts.google.com" in hosts
    assert not any("claude" in h or "anthropic" in h for h in hosts)
    assert (Path(temp) / "Local State").exists()
    assert stat.S_IMODE(os.stat(ck).st_mode) == 0o600
    auth_login.cleanup_reauth_profile(temp)
    assert not os.path.exists(temp)


def test_prepare_missing_source_raises(tmp_path, monkeypatch):
    """Verify prepare_reauth_profile raises FileNotFoundError if source Cookies DB is missing."""
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path / "home"))
    with pytest.raises(FileNotFoundError):
        auth_login.prepare_reauth_profile(str(tmp_path / "nope"))


def test_cleanup_refuses_outside_reauth_tmp(tmp_path, monkeypatch):
    """Verify cleanup_reauth_profile refuses to delete paths outside reauth-tmp."""
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path / "home"))
    victim = tmp_path / "victim"
    victim.mkdir()
    auth_login.cleanup_reauth_profile(str(victim))  # must be a no-op, NOT delete
    assert victim.exists()
