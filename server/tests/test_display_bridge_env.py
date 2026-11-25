"""The SOURCE host's view of the display bridge (spec §8 "Session env on the source").

Real AF_UNIX listeners in a short temp dir stand in for the forwarded sockets the target's
bridge ssh creates; ``x11_dir`` points the X socket lookup at tmp_path instead of
/tmp/.X11-unix.
"""
from __future__ import annotations

import os
import shutil
import socket
import tempfile
from pathlib import Path

import pytest

from server.sessions.display_bridge_env import (
    BRIDGE_DIR, REASON_BRIDGE_UNAVAILABLE, REASON_BRIDGE_WRONG_USER, SOURCE_WAYLAND_DISPLAY,
    bridge_x_display, remote_env)


def _listen(path) -> socket.socket:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(path))
    s.listen(8)
    return s


@pytest.fixture
def layout():
    # A short root: pytest's tmp_path (longer still under xdist) can exceed the 108-byte
    # AF_UNIX path limit.
    root = Path(tempfile.mkdtemp(prefix="vpbe-", dir="/tmp"))
    xrd = root / "xrd"; xrd.mkdir(mode=0o700)
    (xrd / BRIDGE_DIR).mkdir(mode=0o700)
    x11 = root / "x11"; x11.mkdir()
    socks: list[socket.socket] = []
    yield xrd, x11, socks
    for s in socks:
        s.close()
    shutil.rmtree(root, ignore_errors=True)


def _cookie(xrd):
    p = xrd / BRIDGE_DIR / "xauth"
    p.write_bytes(b"\x01")
    p.chmod(0o600)
    return p


def test_remote_env_both_legs_live(layout):
    xrd, x11, socks = layout
    socks.append(_listen(xrd / SOURCE_WAYLAND_DISPLAY))
    socks.append(_listen(x11 / "X150"))
    cookie = _cookie(xrd)
    env = remote_env(runtime_dir=str(xrd), x_display=150, uid=os.getuid(), x11_dir=str(x11))
    assert env == {"WAYLAND_DISPLAY": SOURCE_WAYLAND_DISPLAY, "XDG_RUNTIME_DIR": str(xrd),
                   "DISPLAY": ":150", "XAUTHORITY": str(cookie)}


def test_remote_env_wayland_only_when_x11_absent(layout):
    xrd, x11, socks = layout
    socks.append(_listen(xrd / SOURCE_WAYLAND_DISPLAY))
    _cookie(xrd)
    env = remote_env(runtime_dir=str(xrd), x_display=150, uid=os.getuid(), x11_dir=str(x11))
    assert env == {"WAYLAND_DISPLAY": SOURCE_WAYLAND_DISPLAY, "XDG_RUNTIME_DIR": str(xrd)}
    # X socket live but no cookie file: the X leg is still not exported.
    socks.append(_listen(x11 / "X150"))
    (xrd / BRIDGE_DIR / "xauth").unlink()
    env = remote_env(runtime_dir=str(xrd), x_display=150, uid=os.getuid(), x11_dir=str(x11))
    assert "DISPLAY" not in env
    # The X11 leg switched off: never exported, even when live.
    _cookie(xrd)
    env = remote_env(runtime_dir=str(xrd), x_display=None, uid=os.getuid(), x11_dir=str(x11))
    assert "DISPLAY" not in env and "XAUTHORITY" not in env


def test_remote_env_x11_only_when_wayland_absent(layout):
    xrd, x11, socks = layout
    socks.append(_listen(x11 / "X150"))
    cookie = _cookie(xrd)
    env = remote_env(runtime_dir=str(xrd), x_display=150, uid=os.getuid(), x11_dir=str(x11))
    assert env == {"DISPLAY": ":150", "XAUTHORITY": str(cookie)}


def test_remote_env_dead_socket_is_bridge_unavailable(layout):
    xrd, x11, socks = layout
    dead = _listen(xrd / SOURCE_WAYLAND_DISPLAY)
    dead.close()                                  # the file stays, nobody accepts
    assert (xrd / SOURCE_WAYLAND_DISPLAY).exists()
    assert remote_env(runtime_dir=str(xrd), x_display=150, uid=os.getuid(),
                      x11_dir=str(x11)) == REASON_BRIDGE_UNAVAILABLE
    # Nothing there at all reads the same.
    (xrd / SOURCE_WAYLAND_DISPLAY).unlink()
    assert remote_env(runtime_dir=str(xrd), x_display=150, uid=os.getuid(),
                      x11_dir=str(x11)) == REASON_BRIDGE_UNAVAILABLE
    # A regular file where the socket should be is not a bridge either.
    (xrd / SOURCE_WAYLAND_DISPLAY).write_text("")
    assert remote_env(runtime_dir=str(xrd), x_display=None, uid=os.getuid(),
                      x11_dir=str(x11)) == REASON_BRIDGE_UNAVAILABLE


def test_remote_env_foreign_uid_is_wrong_user(layout):
    xrd, x11, socks = layout
    socks.append(_listen(xrd / SOURCE_WAYLAND_DISPLAY))
    socks.append(_listen(x11 / "X150"))
    _cookie(xrd)
    assert remote_env(runtime_dir=str(xrd), x_display=150, uid=os.getuid() + 1,
                      x11_dir=str(x11)) == REASON_BRIDGE_WRONG_USER


class _S:
    display_bridge_x_start = 150
    display_bridge_x11 = True
    enable_xvfb = True
    display_start = 100
    display_count = 12


def test_x_start_inside_pool_disables_x11_leg():
    assert bridge_x_display(_S()) == 150
    assert bridge_x_display(type("S", (_S,), {"display_count": 60})()) is None
    # Outside the pool when Xvfb is off, whatever the range says.
    assert bridge_x_display(type("S", (_S,), {"display_count": 60, "enable_xvfb": False})()) == 150
    assert bridge_x_display(type("S", (_S,), {"display_bridge_x11": False})()) is None
