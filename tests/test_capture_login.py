"""Tests for auth_login.capture_login (Task 4).

The fake claude binary writes a dummy .credentials.json into $CLAUDE_CONFIG_DIR
and exits 0, so no real claude binary or network is needed.
"""
import json
import socket
import sys
from pathlib import Path

from viberpit import auth_login, paths

FAKE = str(
    Path(__file__).parent.parent / "server" / "tests" / "fakes" / "fake_claude_login.py"
)


def test_capture_login_stores_slot(tmp_path, monkeypatch):
    monkeypatch.setenv("VIBERPIT_HOME", str(tmp_path / "home"))
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
    monkeypatch.setenv("VIBERPIT_HOME", str(tmp_path / "home"))
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
    monkeypatch.setenv("VIBERPIT_HOME", str(tmp_path / "home"))
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
    monkeypatch.setenv("VIBERPIT_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "no-bus-here"))
    auth_login.capture_login("primary", claude_bin=[sys.executable, str(_env_dumping_fake(tmp_path))])
    assert "DBUS_SESSION_BUS_ADDRESS" not in _seen_env()


def test_capture_login_drops_wayland_env_when_display_given(tmp_path, monkeypatch):
    monkeypatch.setenv("VIBERPIT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("WAYLAND_SOCKET", "3")
    auth_login.capture_login(
        "primary",
        claude_bin=[sys.executable, str(_env_dumping_fake(tmp_path))],
        env_extra={"DISPLAY": ":100", "XDG_SESSION_TYPE": "x11"},
    )
    seen = _seen_env()
    assert "WAYLAND_DISPLAY" not in seen and "WAYLAND_SOCKET" not in seen
    assert seen["DISPLAY"] == ":100" and seen["XDG_SESSION_TYPE"] == "x11"


# ── What a captured login stores (``install_claude_login``) ────────────────────────────


def _identity_fake(tmp_path, *, uuid: str | None, email: str | None):
    """A fake `claude auth login` that signs in as the given claude.ai identity."""
    acct = {k: v for k, v in (("accountUuid", uuid), ("emailAddress", email)) if v}
    fake = tmp_path / "fake_claude_identity.py"
    fake.write_text(
        "import json, os, sys\n"
        "cfg = os.environ['CLAUDE_CONFIG_DIR']\n"
        "os.makedirs(cfg, exist_ok=True)\n"
        "json.dump({'claudeAiOauth': {'accessToken': 'NEW-AT', 'refreshToken': 'NEW-RT',\n"
        "           'expiresAt': 9999999999000, 'subscriptionType': 'max'}},\n"
        "          open(os.path.join(cfg, '.credentials.json'), 'w'))\n"
        f"json.dump({{'oauthAccount': {acct!r}}}, open(os.path.join(cfg, '.claude.json'), 'w'))\n"
        "sys.exit(0)\n"
    )
    return [sys.executable, str(fake)]


def _existing(tmp_path, monkeypatch, *, uuid="uuid-A", email="a@example.com"):
    monkeypatch.setenv("VIBERPIT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "dotclaude"))
    paths.store_dir().mkdir(parents=True, exist_ok=True)
    acct = {k: v for k, v in (("accountUuid", uuid), ("emailAddress", email)) if v}
    paths.store_claude_json_path("acct").write_text(json.dumps({"oauthAccount": acct}))
    paths.slot_path("acct").write_text(json.dumps({"accessToken": "OLD-AT"}))


def test_the_add_flow_never_checks_identity(tmp_path, monkeypatch):
    _existing(tmp_path, monkeypatch)
    assert auth_login.capture_login(
        "acct", claude_bin=_identity_fake(tmp_path, uuid="uuid-B", email="b@example.com"))


def test_a_captured_login_clears_the_slot_refresh_rejection(tmp_path, monkeypatch):
    from viberpit import ledger
    _existing(tmp_path, monkeypatch)
    with ledger.locked_ledger() as led:
        led["accounts"].setdefault("acct", {})["slotRefreshRejected"] = True
    auth_login.capture_login("acct",
                             claude_bin=_identity_fake(tmp_path, uuid="uuid-A", email=None))
    assert "slotRefreshRejected" not in ledger.read_ledger()["accounts"].get("acct", {})


def test_a_captured_login_merges_into_the_stored_claude_json(tmp_path, monkeypatch):
    """``install_claude_login`` (shared with the code-paste sign-in) merges the captured
    top-level keys over the stored file: the captured ``oauthAccount`` wins, other
    stored keys are kept."""
    _existing(tmp_path, monkeypatch)
    stored = json.loads(paths.store_claude_json_path("acct").read_text())
    stored["userID"] = "keep-me"
    paths.store_claude_json_path("acct").write_text(json.dumps(stored))
    assert auth_login.capture_login(
        "acct", claude_bin=_identity_fake(tmp_path, uuid="uuid-A", email="a@example.com"))
    merged = json.loads(paths.store_claude_json_path("acct").read_text())
    assert merged["userID"] == "keep-me"
    assert merged["oauthAccount"] == {"accountUuid": "uuid-A", "emailAddress": "a@example.com"}
    assert "NEW-AT" not in paths.store_claude_json_path("acct").read_text()


def test_capture_login_keeps_wayland_env_without_display(tmp_path, monkeypatch):
    monkeypatch.setenv("VIBERPIT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    auth_login.capture_login("primary", claude_bin=[sys.executable, str(_env_dumping_fake(tmp_path))])
    assert _seen_env()["WAYLAND_DISPLAY"] == "wayland-0"
