"""Tests for auth_login.capture_login (Task 4).

The fake claude binary writes a dummy .credentials.json into $CLAUDE_CONFIG_DIR
and exits 0, so no real claude binary or network is needed.
"""
import json
import socket
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
    # account cfg dir was composed
    assert paths.account_cfg_dir("primary").exists()
    # capture dir was under reauth-tmp and is cleaned up
    leftovers = list(paths.reauth_tmp_dir().glob("*")) if paths.reauth_tmp_dir().exists() else []
    assert not any("capture" in p.name for p in leftovers)


def _env_dumping_fake(tmp_path):
    """A fake `claude` that records its environment, then behaves like the real fake."""
    fake = tmp_path / "fake_claude_env.py"
    fake.write_text(
        "import json, os, sys\n"
        "cfg = os.environ['CLAUDE_CONFIG_DIR']\n"
        "os.makedirs(cfg, exist_ok=True)\n"
        "json.dump(dict(os.environ), open(os.path.join(cfg, '..', 'seen-env.json'), 'w'))\n"
        "json.dump({'claudeAiOauth': {'accessToken': 'a', 'refreshToken': 'r',\n"
        "           'expiresAt': 9999999999000, 'subscriptionType': 'max'}},\n"
        "          open(os.path.join(cfg, '.credentials.json'), 'w'))\n"
        "sys.exit(0)\n"
    )
    return fake


def _seen_env():
    return json.load(open(paths.reauth_tmp_dir() / "seen-env.json"))


def test_capture_login_injects_session_bus(tmp_path, monkeypatch):
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(runtime / "bus"))
    try:
        ok = auth_login.capture_login("primary", claude_bin=[sys.executable, str(_env_dumping_fake(tmp_path))])
    finally:
        s.close()
    assert ok is True
    seen = _seen_env()
    assert seen["DBUS_SESSION_BUS_ADDRESS"] == f"unix:path={runtime / 'bus'}"
    assert seen["XDG_RUNTIME_DIR"] == str(runtime)


def test_capture_login_env_extra_beats_injected_bus(tmp_path, monkeypatch):
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(runtime / "bus"))
    try:
        auth_login.capture_login(
            "primary",
            claude_bin=[sys.executable, str(_env_dumping_fake(tmp_path))],
            env_extra={"DBUS_SESSION_BUS_ADDRESS": "unix:path=/explicit"},
        )
    finally:
        s.close()
    assert _seen_env()["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/explicit"


def test_capture_login_without_bus_leaves_env_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "no-bus-here"))
    auth_login.capture_login("primary", claude_bin=[sys.executable, str(_env_dumping_fake(tmp_path))])
    assert "DBUS_SESSION_BUS_ADDRESS" not in _seen_env()
