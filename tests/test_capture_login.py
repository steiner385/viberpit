"""Tests for auth_login.capture_login (Task 4).

The fake claude binary writes a dummy .credentials.json into $CLAUDE_CONFIG_DIR
and exits 0, so no real claude binary or network is needed.
"""
import os
import sys
from pathlib import Path

from ccswitch import auth_login, paths

FAKE = str(
    Path(__file__).parent.parent / "server" / "tests" / "fakes" / "fake_claude_login.py"
)


def test_capture_login_stores_slot(tmp_path, monkeypatch):
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "dotclaude"))
    ok = auth_login.capture_login("primary", claude_bin=[sys.executable, FAKE])
    assert ok is True
    assert paths.slot_path("primary").exists()
    # capture dir was under reauth-tmp and is cleaned up
    leftovers = list(paths.reauth_tmp_dir().glob("*")) if paths.reauth_tmp_dir().exists() else []
    assert not any("capture" in p.name for p in leftovers)
