"""Tests for auth_login.capture_setup_token (Phase B1).

Uses fake claude binaries — no real claude binary, no network, no PTY colour
codes.  The test sets CCSWITCH_MASTER_KEY so encryption works, and monkeypatches
paths.store_dir + paths.reauth_tmp_dir to tmp_path so nothing touches the real
store.
"""
from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

# A deterministic 32-byte test key (matches the convention in test_crypto.py).
_TEST_KEY_B64 = base64.b64encode(bytes(range(32))).decode("ascii")

FAKE_OK = str(
    Path(__file__).parent / "fakes" / "fake_claude_setup_token.py"
)
FAKE_NOTOKEN = str(
    Path(__file__).parent / "fakes" / "fake_claude_setup_token_notoken.py"
)
FAKE_HANG = str(
    Path(__file__).parent / "fakes" / "fake_claude_setup_token_hang.py"
)


def test_capture_setup_token_captures_and_stores(monkeypatch, tmp_path):
    """Happy path: URL surfaced → code sent → token extracted and stored encrypted."""
    old_key = os.environ.get("CCSWITCH_MASTER_KEY")
    os.environ["CCSWITCH_MASTER_KEY"] = _TEST_KEY_B64
    try:
        from ccswitch import auth_login, crypto, paths

        monkeypatch.setattr(paths, "store_dir", lambda: tmp_path)
        monkeypatch.setattr(paths, "reauth_tmp_dir", lambda: tmp_path)

        seen: dict[str, str] = {}

        ok = auth_login.capture_setup_token(
            "acct1",
            on_url=lambda u: seen.__setitem__("url", u),
            get_code=lambda: "FAKECODE#fakestate",
            claude_bin=[sys.executable, FAKE_OK],
        )

        assert ok is True, "capture_setup_token should return True on success"
        assert "claude.com/cai/oauth/authorize" in seen.get("url", ""), (
            f"on_url callback should receive the authorize URL; got: {seen!r}"
        )

        tok = crypto.read_oauth_token("acct1")
        assert tok == "sk-ant-oat01-FAKETOKEN0123456789", (
            f"read_oauth_token should return the plaintext token; got: {tok!r}"
        )

        # Verify it is stored ENCRYPTED at rest (not plaintext)
        raw = (tmp_path / "oat" / "acct1").read_text()
        assert "sk-ant-oat01-FAKETOKEN0123456789" not in raw, (
            "Token must NOT appear in plaintext in the encrypted file"
        )
        assert raw.startswith("ccenc1:"), (
            f"File should start with ccenc1: sentinel; got: {raw[:40]!r}"
        )
    finally:
        if old_key is None:
            os.environ.pop("CCSWITCH_MASTER_KEY", None)
        else:
            os.environ["CCSWITCH_MASTER_KEY"] = old_key


def test_capture_setup_token_newline_does_not_submit(monkeypatch, tmp_path):
    """Regression contract for the \\n→\\r fix: if the code is submitted with a
    newline instead of a carriage-return, claude's raw-mode TUI never registers
    Enter, no token is produced, and capture_setup_token returns False.

    Proven live: claude only submits on \\r. This locks that contract as a
    machine-checkable test (the happy-path test already breaks if \\r is reverted,
    but this asserts the failure direction explicitly)."""
    old_key = os.environ.get("CCSWITCH_MASTER_KEY")
    os.environ["CCSWITCH_MASTER_KEY"] = _TEST_KEY_B64
    try:
        from ccswitch import auth_login, crypto, paths

        monkeypatch.setattr(paths, "store_dir", lambda: tmp_path)
        monkeypatch.setattr(paths, "reauth_tmp_dir", lambda: tmp_path)

        # Downgrade ONLY the code-submission write (\r → \n) to simulate the
        # pre-fix behavior; leave every other os.write untouched.
        real_write = auth_login.os.write

        def write_downgrading_cr(fd, data):
            if b"FAKECODE" in data:
                data = data.replace(b"\r", b"\n")
            return real_write(fd, data)

        monkeypatch.setattr(auth_login.os, "write", write_downgrading_cr)

        ok = auth_login.capture_setup_token(
            "acct_lf",
            on_url=lambda u: None,
            get_code=lambda: "FAKECODE#fakestate",
            claude_bin=[sys.executable, FAKE_OK],
            post_code_timeout_s=8.0,
        )

        assert ok is False, "submitting the code with \\n (not \\r) must NOT mint a token"
        assert crypto.read_oauth_token("acct_lf") is None, (
            "no token should be stored when the code was never submitted"
        )
    finally:
        if old_key is None:
            os.environ.pop("CCSWITCH_MASTER_KEY", None)
        else:
            os.environ["CCSWITCH_MASTER_KEY"] = old_key


def test_capture_setup_token_returns_false_when_no_token(monkeypatch, tmp_path):
    """Failure path: fake binary prints no sk-ant-oat01 token → returns False."""
    old_key = os.environ.get("CCSWITCH_MASTER_KEY")
    os.environ["CCSWITCH_MASTER_KEY"] = _TEST_KEY_B64
    try:
        from ccswitch import auth_login, paths

        monkeypatch.setattr(paths, "store_dir", lambda: tmp_path)
        monkeypatch.setattr(paths, "reauth_tmp_dir", lambda: tmp_path)

        ok = auth_login.capture_setup_token(
            "acct2",
            on_url=lambda u: None,
            get_code=lambda: "BADCODE",
            claude_bin=[sys.executable, FAKE_NOTOKEN],
        )

        assert ok is False, "capture_setup_token should return False when no token found"
        # No encrypted file should exist
        oat_file = tmp_path / "oat" / "acct2"
        assert not oat_file.exists(), "No token file should be created on failure"
    finally:
        if old_key is None:
            os.environ.pop("CCSWITCH_MASTER_KEY", None)
        else:
            os.environ["CCSWITCH_MASTER_KEY"] = old_key


def test_capture_setup_token_on_pid_receives_child_pid(monkeypatch, tmp_path):
    """Test C: on_pid callback is invoked with the spawned child's PID (positive int)."""
    old_key = os.environ.get("CCSWITCH_MASTER_KEY")
    os.environ["CCSWITCH_MASTER_KEY"] = _TEST_KEY_B64
    try:
        from ccswitch import auth_login, paths

        monkeypatch.setattr(paths, "store_dir", lambda: tmp_path)
        monkeypatch.setattr(paths, "reauth_tmp_dir", lambda: tmp_path)

        pids: list[int] = []

        ok = auth_login.capture_setup_token(
            "acct_pid",
            on_url=lambda u: None,
            get_code=lambda: "FAKECODE#fakestate",
            claude_bin=[sys.executable, FAKE_OK],
            on_pid=lambda pid: pids.append(pid),
        )

        assert ok is True, "capture_setup_token should return True on success"
        assert len(pids) == 1, f"on_pid should be called exactly once; got {pids!r}"
        assert pids[0] > 0, f"on_pid should receive a positive PID; got {pids[0]!r}"
    finally:
        if old_key is None:
            os.environ.pop("CCSWITCH_MASTER_KEY", None)
        else:
            os.environ["CCSWITCH_MASTER_KEY"] = old_key


def test_capture_setup_token_bounded_failure_returns_false(monkeypatch, tmp_path):
    """Test B: hang/invalid-code fake returns False within post_code_timeout_s, not hanging."""
    import time

    old_key = os.environ.get("CCSWITCH_MASTER_KEY")
    os.environ["CCSWITCH_MASTER_KEY"] = _TEST_KEY_B64
    try:
        from ccswitch import auth_login, paths

        monkeypatch.setattr(paths, "store_dir", lambda: tmp_path)
        monkeypatch.setattr(paths, "reauth_tmp_dir", lambda: tmp_path)

        t0 = time.monotonic()
        ok = auth_login.capture_setup_token(
            "acct_hang",
            on_url=lambda u: None,
            get_code=lambda: "FAKECODE#fakestate",
            claude_bin=[sys.executable, FAKE_HANG],
            post_code_timeout_s=2.0,
        )
        elapsed = time.monotonic() - t0

        assert ok is False, "capture_setup_token should return False for non-completing claude"
        assert elapsed < 5.0, (
            f"capture_setup_token should return within ~5s; took {elapsed:.1f}s"
        )
        # No token file should be created
        oat_file = tmp_path / "oat" / "acct_hang"
        assert not oat_file.exists(), "No token file should be created on bounded failure"
    finally:
        if old_key is None:
            os.environ.pop("CCSWITCH_MASTER_KEY", None)
        else:
            os.environ["CCSWITCH_MASTER_KEY"] = old_key
