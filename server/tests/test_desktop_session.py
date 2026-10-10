"""Live desktop-session resolver: fake /proc trees + a fake X server socket."""
from __future__ import annotations

import os
import shutil
import socket
import struct
import subprocess
import threading
import time
from pathlib import Path

import pytest

from server.sessions import desktop_session as ds
from server.sessions.desktop_session import (
    REASON_CONNECT_REFUSED,
    REASON_COOKIE_MISSING,
    REASON_INVALID_DISPLAY,
    REASON_NO_GRAPHICAL_SESSION,
    DesktopSession,
    DesktopUnavailable,
    probe_x11,
    resolve_desktop_session,
)

UID = os.getuid()
SD: Path  # set per-test by _sd


@pytest.fixture(autouse=True)
def _sd(tmp_path: Path):
    global SD
    SD = tmp_path / "x11"
    yield


def _proc(root: Path, pid: int, comm: str, argv: list[str], environ: dict[str, str] | None = None,
          uid: int | None = None) -> None:
    uid = UID if uid is None else uid
    d = root / str(pid)
    d.mkdir()
    (d / "comm").write_text(comm + "\n")
    (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    (d / "status").write_text(f"Name:\t{comm}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n")
    if environ is not None:
        (d / "environ").write_bytes(b"\0".join(f"{k}={v}".encode() for k, v in environ.items()) + b"\0")


@pytest.fixture(autouse=True)
def _socks(tmp_path: Path):
    """Real AF_UNIX socket files X0..X3 (owned by us) so the socket-trust check passes."""
    d = tmp_path / "x11"
    d.mkdir()
    held = []
    for n in range(4):
        s = socket.socket(socket.AF_UNIX)
        s.bind(str(d / f"X{n}"))
        held.append(s)
    yield
    for s in held:
        s.close()


def _ok(_sock: str, _xauth: str | None) -> bool:
    return True


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "proc"
    r.mkdir()
    return r


def _cookie(tmp_path: Path, name: str = "cookie") -> str:
    p = tmp_path / name
    p.write_bytes(b"x")
    return str(p)


def test_wayland_xwayland_with_environ(root: Path, tmp_path: Path) -> None:
    ck = _cookie(tmp_path)
    _proc(root, 10, "Xwayland", ["/usr/bin/Xwayland", ":0", "-rootless", "-auth", ck, "-listenfd", "4"],
          {"DISPLAY": ":0", "XAUTHORITY": ck, "WAYLAND_DISPLAY": "wayland-0", "XDG_RUNTIME_DIR": "/run/user/1000"})
    got = resolve_desktop_session(uid=UID, socket_dir=str(SD), proc_root=str(root), probe=_ok)
    assert got == DesktopSession("wayland", ":0", ck, "wayland-0", "/run/user/1000")


def test_xorg_legacy_with_auth(root: Path, tmp_path: Path) -> None:
    ck = _cookie(tmp_path)
    _proc(root, 11, "Xorg", ["/usr/lib/Xorg", ":0", "-auth", ck, "vt1"])
    got = resolve_desktop_session(uid=UID, socket_dir=str(SD), proc_root=str(root), probe=_ok)
    assert got == DesktopSession("x11", ":0", ck, None, None)


def test_xorg_without_auth_is_legacy_posture(root: Path) -> None:
    _proc(root, 11, "Xorg", ["Xorg", ":0"])
    got = resolve_desktop_session(uid=UID, socket_dir=str(SD), proc_root=str(root), probe=_ok)
    assert isinstance(got, DesktopSession) and got.xauthority is None and got.kind == "x11"


def test_cookie_named_but_missing(root: Path, tmp_path: Path) -> None:
    _proc(root, 11, "Xorg", ["Xorg", ":0", "-auth", str(tmp_path / "gone")])
    got = resolve_desktop_session(uid=UID, socket_dir=str(SD), proc_root=str(root), probe=_ok)
    assert isinstance(got, DesktopUnavailable) and got.reason == REASON_COOKIE_MISSING


def test_cookie_from_environ_when_no_auth_arg(root: Path, tmp_path: Path) -> None:
    ck = _cookie(tmp_path)
    _proc(root, 10, "Xwayland", ["Xwayland", ":0"], {"XAUTHORITY": ck, "WAYLAND_DISPLAY": "wayland-0"})
    got = resolve_desktop_session(uid=UID, socket_dir=str(SD), proc_root=str(root), probe=_ok)
    assert isinstance(got, DesktopSession) and got.xauthority == ck


def test_no_server(root: Path) -> None:
    _proc(root, 5, "bash", ["bash"])
    got = resolve_desktop_session(uid=UID, socket_dir=str(SD), proc_root=str(root), probe=_ok)
    assert isinstance(got, DesktopUnavailable) and got.reason == REASON_NO_GRAPHICAL_SESSION


def test_invalid_display(root: Path) -> None:
    _proc(root, 11, "Xorg", ["Xorg", ":0"])
    got = resolve_desktop_session(uid=UID, socket_dir=str(SD), display="host:0", proc_root=str(root), probe=_ok)
    assert isinstance(got, DesktopUnavailable) and got.reason == REASON_INVALID_DISPLAY


def test_requested_display_absent(root: Path) -> None:
    _proc(root, 11, "Xorg", ["Xorg", ":0"])
    got = resolve_desktop_session(uid=UID, socket_dir=str(SD), display=":1", proc_root=str(root), probe=_ok)
    assert isinstance(got, DesktopUnavailable) and got.reason == REASON_NO_GRAPHICAL_SESSION


def test_requested_display_with_screen_suffix(root: Path) -> None:
    _proc(root, 11, "Xorg", ["Xorg", ":1"])
    got = resolve_desktop_session(uid=UID, socket_dir=str(SD), display=":1.0", proc_root=str(root), probe=_ok)
    assert isinstance(got, DesktopSession) and got.display == ":1"


def test_lowest_display_wins_without_request(root: Path) -> None:
    _proc(root, 12, "Xorg", ["Xorg", ":2"])
    _proc(root, 11, "Xorg", ["Xorg", ":1"])
    got = resolve_desktop_session(uid=UID, socket_dir=str(SD), proc_root=str(root), probe=_ok)
    assert isinstance(got, DesktopSession) and got.display == ":1"


def test_unreadable_entries_skipped(root: Path) -> None:
    (root / "99").mkdir()  # no comm/cmdline: raced exit
    (root / "self").mkdir()
    _proc(root, 11, "Xorg", ["Xorg", ":0"])
    assert isinstance(resolve_desktop_session(uid=UID, socket_dir=str(SD), proc_root=str(root), probe=_ok), DesktopSession)


def test_probe_false_is_connect_refused(root: Path, tmp_path: Path) -> None:
    _proc(root, 11, "Xorg", ["Xorg", ":3"])
    seen: list[tuple[str, str | None]] = []

    def probe(sock: str, xauth: str | None) -> bool:
        seen.append((sock, xauth))
        return False

    got = resolve_desktop_session(uid=UID, socket_dir=str(SD), proc_root=str(root), probe=probe)
    assert isinstance(got, DesktopUnavailable) and got.reason == REASON_CONNECT_REFUSED
    assert seen == [(str(SD / "X3"), None)]


def _owners(root: Path, by_pid: dict[str, int]) -> None:
    for pid, uid in by_pid.items():
        (root / pid / "status").write_text(f"Uid:\t{uid}\t{uid}\t{uid}\t{uid}\n")


def test_other_uid_server_ignored(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _proc(root, 11, "Xorg", ["Xorg", ":0", "-auth", "/etc/passwd"])
    _owners(root, {"11": UID + 1})
    got = resolve_desktop_session(uid=UID, socket_dir=str(SD), display=":0", proc_root=str(root), probe=_ok)
    assert isinstance(got, DesktopUnavailable) and got.reason == REASON_NO_GRAPHICAL_SESSION


def test_root_owned_server_accepted(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _proc(root, 11, "Xorg", ["Xorg", ":0"])
    _owners(root, {"11": 0})
    got = resolve_desktop_session(uid=UID, socket_dir=str(SD), proc_root=str(root), probe=_ok)
    assert isinstance(got, DesktopSession) and got.kind == "x11"


def test_uid_owned_preferred_over_root(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _proc(root, 11, "Xorg", ["Xorg", ":0"])
    _proc(root, 12, "Xorg", ["Xorg", ":1"])
    _owners(root, {"11": 0, "12": UID})
    got = resolve_desktop_session(uid=UID, socket_dir=str(SD), proc_root=str(root), probe=_ok)
    assert isinstance(got, DesktopSession) and got.display == ":1"


def test_socket_owned_by_other_uid_rejected(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _proc(root, 11, "Xorg", ["Xorg", ":0"])
    monkeypatch.setattr(ds, "socket_owner", lambda p: UID + 1)
    got = resolve_desktop_session(uid=UID, socket_dir=str(SD), proc_root=str(root), probe=_ok)
    assert isinstance(got, DesktopUnavailable) and got.reason == REASON_NO_GRAPHICAL_SESSION


def test_socket_symlink_rejected(root: Path, tmp_path: Path) -> None:
    _proc(root, 11, "Xorg", ["Xorg", ":9"])
    (SD / "X9").symlink_to(SD / "X0")  # points at a real socket we own
    got = resolve_desktop_session(uid=UID, socket_dir=str(SD), proc_root=str(root), probe=_ok)
    assert isinstance(got, DesktopUnavailable) and got.reason == REASON_NO_GRAPHICAL_SESSION


def test_regular_file_socket_rejected(root: Path) -> None:
    _proc(root, 11, "Xorg", ["Xorg", ":8"])
    (SD / "X8").write_text("")
    got = resolve_desktop_session(uid=UID, socket_dir=str(SD), proc_root=str(root), probe=_ok)
    assert isinstance(got, DesktopUnavailable) and got.reason == REASON_NO_GRAPHICAL_SESSION


def test_unreadable_cookie_falls_back_to_legacy(root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ck = _cookie(tmp_path)
    _proc(root, 11, "Xorg", ["Xorg", ":0", "-auth", ck])
    real = ds._read
    monkeypatch.setattr(ds, "_read", lambda p: None if p == ck else real(p))
    seen: list[str | None] = []
    def probe(_sock: str, cookie: str | None) -> bool:
        seen.append(cookie)
        return True

    got = resolve_desktop_session(uid=UID, socket_dir=str(SD), proc_root=str(root), probe=probe)
    assert isinstance(got, DesktopSession) and got.xauthority is None and seen == [None]


def test_xwayland_unreadable_environ_is_x11(root: Path) -> None:
    _proc(root, 10, "Xwayland", ["Xwayland", ":0"])  # no environ file
    got = resolve_desktop_session(uid=UID, socket_dir=str(SD), proc_root=str(root), probe=_ok)
    assert got == DesktopSession("x11", ":0", None, None, None)
    assert "WAYLAND_DISPLAY" not in got.env(prefer_wayland=True)


def test_cookie_prefers_local_over_earlier_foreign_family(tmp_path: Path) -> None:
    xa = tmp_path / "xa"
    xa.write_bytes(
        _xauth_record(0, b"\x0a\x00\x00\x01", b"3", b"MIT-MAGIC-COOKIE-1", b"F" * 16)
        + _xauth_record(0x0100, b"host", b"3", b"MIT-MAGIC-COOKIE-1", b"L" * 16))
    assert ds._cookie_for(str(xa), "3") == b"L" * 16


def test_cookie_falls_back_to_foreign_family_when_no_local(tmp_path: Path) -> None:
    xa = tmp_path / "xa"
    xa.write_bytes(_xauth_record(0, b"\x0a\x00\x00\x01", b"3", b"MIT-MAGIC-COOKIE-1", b"F" * 16))
    assert ds._cookie_for(str(xa), "3") == b"F" * 16


def test_truncated_record_after_valid_keeps_valid_cookie(tmp_path: Path) -> None:
    xa = tmp_path / "xa"
    good = _xauth_record(0x0100, b"host", b"3", b"MIT-MAGIC-COOKIE-1", b"G" * 16)
    xa.write_bytes(good + _xauth_record(0x0100, b"host", b"3", b"MIT-MAGIC-COOKIE-1", b"H" * 16)[:-5])
    assert ds._cookie_for(str(xa), "3") == b"G" * 16


def test_env_wayland() -> None:
    s = DesktopSession("wayland", ":0", "/ck", "wayland-0", "/run/user/1000")
    assert s.env(prefer_wayland=True) == {
        "DISPLAY": ":0", "XAUTHORITY": "/ck", "WAYLAND_DISPLAY": "wayland-0",
        "XDG_RUNTIME_DIR": "/run/user/1000", "XDG_SESSION_TYPE": "wayland"}
    assert s.env(prefer_wayland=False) == {"DISPLAY": ":0", "XAUTHORITY": "/ck", "XDG_SESSION_TYPE": "x11"}


def test_env_x11() -> None:
    s = DesktopSession("x11", ":1", None, None, None)
    expected = {"DISPLAY": ":1", "XDG_SESSION_TYPE": "x11"}
    assert s.env(prefer_wayland=True) == expected
    assert s.env(prefer_wayland=False) == expected


# ---- probe_x11 -------------------------------------------------------------

def _xauth_record(family: int, addr: bytes, number: bytes, name: bytes, data: bytes) -> bytes:
    out = struct.pack(">H", family)
    for field in (addr, number, name, data):
        out += struct.pack(">H", len(field)) + field
    return out


class FakeX:
    def __init__(self, path: Path, reply: bytes) -> None:
        self.path, self.reply = path, reply
        self.request = b""
        self._srv = socket.socket(socket.AF_UNIX)
        self._srv.bind(str(path))
        self._srv.listen(1)
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _run(self) -> None:
        conn, _ = self._srv.accept()
        with conn:
            conn.settimeout(2)
            buf = b""
            while len(buf) < 12:
                buf += conn.recv(4096)
            nlen, dlen = struct.unpack("<HH", buf[6:10])
            need = 12 + (nlen + 3) // 4 * 4 + (dlen + 3) // 4 * 4
            while len(buf) < need:
                buf += conn.recv(4096)
            self.request = buf
            conn.sendall(self.reply)

    def close(self) -> None:
        self._srv.close()
        self._t.join(timeout=2)


def test_probe_sends_cookie_and_accepts(tmp_path: Path) -> None:
    cookie = bytes(range(16))
    xa = tmp_path / "xa"
    xa.write_bytes(
        _xauth_record(0x0100, b"host", b"7", b"MIT-MAGIC-COOKIE-1", b"\xff" * 16)
        + _xauth_record(0x0100, b"host", b"3", b"MIT-MAGIC-COOKIE-1", cookie))
    fx = FakeX(tmp_path / "X3", b"\x01" + b"\0" * 7)
    try:
        assert probe_x11(str(fx.path), str(xa)) is True
    finally:
        fx.close()
    req = fx.request
    assert req[0] == 0x6C and struct.unpack("<HH", req[2:6]) == (11, 0)
    assert struct.unpack("<HH", req[6:10]) == (18, 16)
    assert req[12:12 + 18] == b"MIT-MAGIC-COOKIE-1"
    assert req[12 + 20:12 + 20 + 16] == cookie


def test_probe_wild_family_and_empty_number(tmp_path: Path) -> None:
    xa = tmp_path / "xa"
    xa.write_bytes(_xauth_record(0xFFFF, b"", b"", b"MIT-MAGIC-COOKIE-1", b"A" * 16))
    fx = FakeX(tmp_path / "X0", b"\x01")
    try:
        assert probe_x11(str(fx.path), str(xa)) is True
    finally:
        fx.close()
    assert fx.request[12 + 20:12 + 36] == b"A" * 16


def test_probe_no_cookie_sends_empty_auth(tmp_path: Path) -> None:
    fx = FakeX(tmp_path / "X0", b"\x01")
    try:
        assert probe_x11(str(fx.path), None) is True
    finally:
        fx.close()
    assert struct.unpack("<HH", fx.request[6:10]) == (0, 0)


@pytest.mark.parametrize("reply", [b"\x00", b"\x02"])
def test_probe_denied(tmp_path: Path, reply: bytes) -> None:
    fx = FakeX(tmp_path / "X0", reply)
    try:
        assert probe_x11(str(fx.path), None) is False
    finally:
        fx.close()


def test_probe_nonexistent_socket(tmp_path: Path) -> None:
    assert probe_x11(str(tmp_path / "nope"), None) is False


def test_probe_garbage_xauthority_file(tmp_path: Path) -> None:
    xa = tmp_path / "xa"
    xa.write_bytes(b"\x00\x01\xff")
    fx = FakeX(tmp_path / "X0", b"\x01")
    try:
        assert probe_x11(str(fx.path), str(xa)) is True
    finally:
        fx.close()
    assert struct.unpack("<HH", fx.request[6:10]) == (0, 0)


def test_probe_timeout_when_server_silent(tmp_path: Path) -> None:
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(str(tmp_path / "X0"))
    srv.listen(1)
    try:
        assert probe_x11(str(tmp_path / "X0"), None, timeout_s=0.1) is False
    finally:
        srv.close()


def test_no_module_level_state() -> None:
    assert not [n for n, v in vars(ds).items() if isinstance(v, (dict, list, set)) and not n.startswith("__")]


@pytest.mark.slow
@pytest.mark.skipif(not (shutil.which("Xvfb") and shutil.which("xauth")), reason="needs Xvfb + xauth")
def test_probe_real_xvfb(tmp_path: Path) -> None:
    n = next(i for i in range(90, 200) if not os.path.exists(f"/tmp/.X11-unix/X{i}"))
    xa = tmp_path / "xa"
    xa.touch()
    key = os.urandom(16).hex()
    subprocess.run(["xauth", "-f", str(xa), "add", f":{n}", ".", key], check=True)
    proc = subprocess.Popen(["Xvfb", f":{n}", "-auth", str(xa), "-nolisten", "tcp"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        sock = f"/tmp/.X11-unix/X{n}"
        # Poll the probe itself, like x11manager._await_ready: Xvfb creates the socket
        # before it can answer a connection setup, so a single probe right after the
        # socket appears raced a slow CI runner (master ee737d7, 2026-10-09).
        deadline = time.monotonic() + 15
        ready = False
        while not ready and time.monotonic() < deadline and proc.poll() is None:
            ready = probe_x11(sock, str(xa), timeout_s=2.0)
            if not ready:
                time.sleep(0.1)
        assert proc.poll() is None, f"Xvfb :{n} exited early with {proc.returncode}"
        assert ready, f"Xvfb :{n} never accepted our cookie within 15s"
        assert probe_x11(sock, None) is False
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_dir_owner_is_not_trusted_only_status_uid(root: Path) -> None:
    # Non-dumpable fake Xorg: /proc/<pid> would read root-owned, but its real uid is another user.
    _proc(root, 11, "Xorg", ["Xorg", ":0"], uid=UID + 1)
    got = resolve_desktop_session(uid=UID, socket_dir=str(SD), proc_root=str(root), probe=_ok)
    assert isinstance(got, DesktopUnavailable) and got.reason == REASON_NO_GRAPHICAL_SESSION


def test_unreadable_status_skipped(root: Path) -> None:
    _proc(root, 11, "Xorg", ["Xorg", ":0"])
    (root / "11" / "status").unlink()
    got = resolve_desktop_session(uid=UID, socket_dir=str(SD), proc_root=str(root), probe=_ok)
    assert isinstance(got, DesktopUnavailable) and got.reason == REASON_NO_GRAPHICAL_SESSION


def test_pinned_display_prefers_uid_owned_candidate(root: Path) -> None:
    _proc(root, 10, "Xorg", ["Xorg", ":0"], uid=0)
    _proc(root, 12, "Xorg", ["Xorg", ":0"], uid=UID)
    got = resolve_desktop_session(uid=UID, socket_dir=str(SD), display=":0", proc_root=str(root), probe=_ok)
    assert isinstance(got, DesktopSession)
    assert got.display == ":0"
    # uid-owned server's environ is read; the root one's never is.
    (root / "12" / "environ").write_bytes(b"XAUTHORITY=/nonexistent/cookie\0")
    got = resolve_desktop_session(uid=UID, socket_dir=str(SD), display=":0", proc_root=str(root), probe=_ok)
    assert isinstance(got, DesktopUnavailable) and got.reason == REASON_COOKIE_MISSING
