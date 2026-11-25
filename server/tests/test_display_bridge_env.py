"""The SOURCE host's view of the display bridge (spec §8 "Session env on the source").

Real AF_UNIX listeners stand in for the forwarded sockets the target's bridge ssh
creates inside the source user's private dir.
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
    X11_SOCKET, bridge_x11_enabled, remote_env, x11_socket_path)


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
    socks: list[socket.socket] = []
    yield xrd, socks
    for s in socks:
        s.close()
    shutil.rmtree(root, ignore_errors=True)


def _cookie(xrd):
    p = xrd / BRIDGE_DIR / "xauth"
    p.write_bytes(b"\x01")
    p.chmod(0o600)
    return p


def _x11(xrd):
    return xrd / BRIDGE_DIR / X11_SOCKET


def test_remote_env_both_legs_live(layout):
    xrd, socks = layout
    socks.append(_listen(xrd / SOURCE_WAYLAND_DISPLAY))
    socks.append(_listen(_x11(xrd)))
    cookie = _cookie(xrd)
    env = remote_env(runtime_dir=str(xrd), x11=True, uid=os.getuid())
    # DISPLAY is the socket's absolute path, never ":N": with ":N" libxcb tries the
    # abstract @/tmp/.X11-unix/X<N> first, which any local user can bind.
    assert env == {"WAYLAND_DISPLAY": SOURCE_WAYLAND_DISPLAY, "XDG_RUNTIME_DIR": str(xrd),
                   "DISPLAY": str(_x11(xrd)), "XAUTHORITY": str(cookie)}
    assert x11_socket_path(str(xrd)) == str(_x11(xrd))
    assert "/tmp/.X11-unix" not in env["DISPLAY"] and not env["DISPLAY"].startswith(":")


def test_remote_env_wayland_only_when_x11_absent(layout):
    xrd, socks = layout
    socks.append(_listen(xrd / SOURCE_WAYLAND_DISPLAY))
    _cookie(xrd)
    blank_x = {"WAYLAND_DISPLAY": SOURCE_WAYLAND_DISPLAY, "XDG_RUNTIME_DIR": str(xrd),
               "DISPLAY": "", "XAUTHORITY": ""}
    # No X socket: the X keys are blanked, so an inherited DISPLAY cannot leak through.
    assert remote_env(runtime_dir=str(xrd), x11=True, uid=os.getuid()) == blank_x
    # X socket live but no cookie file: the X leg is still not exported.
    socks.append(_listen(_x11(xrd)))
    (xrd / BRIDGE_DIR / "xauth").unlink()
    assert remote_env(runtime_dir=str(xrd), x11=True, uid=os.getuid()) == blank_x
    # The X11 leg switched off: never exported, even when live.
    _cookie(xrd)
    assert remote_env(runtime_dir=str(xrd), x11=False, uid=os.getuid()) == blank_x


def test_remote_env_x11_only_blanks_wayland(layout):
    xrd, socks = layout
    socks.append(_listen(_x11(xrd)))
    cookie = _cookie(xrd)
    env = remote_env(runtime_dir=str(xrd), x11=True, uid=os.getuid())
    assert env == {"WAYLAND_DISPLAY": "", "DISPLAY": str(_x11(xrd)), "XAUTHORITY": str(cookie)}


def test_remote_env_dead_socket_is_bridge_unavailable(layout):
    xrd, socks = layout
    dead = _listen(xrd / SOURCE_WAYLAND_DISPLAY)
    dead.close()                                  # the file stays, nobody accepts
    assert (xrd / SOURCE_WAYLAND_DISPLAY).exists()
    assert remote_env(runtime_dir=str(xrd), x11=True, uid=os.getuid()) == REASON_BRIDGE_UNAVAILABLE
    # Nothing there at all reads the same.
    (xrd / SOURCE_WAYLAND_DISPLAY).unlink()
    assert remote_env(runtime_dir=str(xrd), x11=True, uid=os.getuid()) == REASON_BRIDGE_UNAVAILABLE
    # A regular file where the socket should be is not a bridge either.
    (xrd / SOURCE_WAYLAND_DISPLAY).write_text("")
    assert remote_env(runtime_dir=str(xrd), x11=False, uid=os.getuid()) == REASON_BRIDGE_UNAVAILABLE


def test_remote_env_foreign_uid_is_wrong_user(layout):
    xrd, socks = layout
    socks.append(_listen(xrd / SOURCE_WAYLAND_DISPLAY))
    socks.append(_listen(_x11(xrd)))
    _cookie(xrd)
    assert remote_env(runtime_dir=str(xrd), x11=True,
                      uid=os.getuid() + 1) == REASON_BRIDGE_WRONG_USER


class _S:
    display_bridge_x11 = True
    enable_xvfb = True
    display_start = 100
    display_count = 100


def test_x11_leg_follows_only_its_setting():
    # The path display carries no display number, so the Xvfb pool can never collide.
    assert bridge_x11_enabled(_S()) is True
    assert bridge_x11_enabled(type("S", (_S,), {"display_bridge_x11": False})()) is False
