"""The SOURCE host's side of the display bridge: is it live here, and what env points at it.

The target daemon's :mod:`server.sessions.display_bridge` forwards its desktop onto this
host over ssh (spec §8 "Verified invocation"). On the source that leaves, per source host
(one target at a time):

* the waypipe server's Wayland socket ``$XDG_RUNTIME_DIR/wayland-viberpit-bridge``;
* the X11 forward ``/tmp/.X11-unix/X<display_bridge_x_start>``;
* the target's cookie ``$XDG_RUNTIME_DIR/viberpit-bridge/xauth`` (0600 in a 0700 dir).

This module owns that layout (the target side imports the names from here) and the one
liveness check: a socket counts only when it exists, is a socket, is owned by the
session user and accepts ``connect()`` — a stale socket file left by a dead bridge, or
one another user squatted in the world-writable ``/tmp/.X11-unix``, must never reach a
session's env. The check blocks for up to ``_CONNECT_TIMEOUT_S`` per leg, so async
callers run it in a worker thread.
"""
from __future__ import annotations

import os
import socket
import stat
from typing import Any

SOURCE_WAYLAND_DISPLAY = "wayland-viberpit-bridge"
BRIDGE_DIR = "viberpit-bridge"
X11_SOCKET_DIR = "/tmp/.X11-unix"

# Fallback reasons (spec Global Constraints; exact strings).
REASON_BRIDGE_UNAVAILABLE = "bridge_unavailable"
REASON_BRIDGE_WRONG_USER = "bridge_wrong_user"

_CONNECT_TIMEOUT_S = 0.5

_LIVE = "live"
_ABSENT = "absent"
_DEAD = "dead"
_FOREIGN = "foreign"


def bridge_x11_disabled_reason(settings: Any) -> str | None:
    """Why this host runs the bridge without its X11 leg, or None when the leg is on.
    The X11 leg is off by setting, or when its display number sits inside the Xvfb pool
    (X11Manager would lease, and its orphan sweep reap, that display)."""
    if not getattr(settings, "display_bridge_x11", True):
        return "VIBERPIT_DISPLAY_BRIDGE_X11=false"
    if getattr(settings, "enable_xvfb", False):
        x_start = int(getattr(settings, "display_bridge_x_start", 150))
        start = int(getattr(settings, "display_start", 100))
        count = int(getattr(settings, "display_count", 12))
        if start <= x_start < start + count:
            return f":{x_start} is inside the Xvfb pool [{start}, {start + count})"
    return None


def bridge_x_display(settings: Any) -> int | None:
    """The forwarded X display number, or None when the X11 leg is off on this host."""
    if bridge_x11_disabled_reason(settings) is not None:
        return None
    return int(getattr(settings, "display_bridge_x_start", 150))


def source_runtime_dir(uid: int) -> str:
    """The session user's ``$XDG_RUNTIME_DIR`` (the daemon runs as that user)."""
    return os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{uid}"


def _socket_state(path: str, uid: int) -> str:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return _ABSENT
    except OSError:
        return _DEAD
    if not stat.S_ISSOCK(st.st_mode):
        return _DEAD
    if st.st_uid != uid:
        return _FOREIGN
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(_CONNECT_TIMEOUT_S)
        s.connect(path)
    except OSError:
        return _DEAD
    finally:
        s.close()
    return _LIVE


def _cookie_ok(path: str, uid: int) -> bool:
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and st.st_uid == uid


def remote_env(*, runtime_dir: str, x_display: int | None, uid: int,
               x11_dir: str = X11_SOCKET_DIR) -> dict[str, str] | str:
    """The env that points a session at the bridge, or why it can't.

    Returns ``WAYLAND_DISPLAY``/``XDG_RUNTIME_DIR`` when the Wayland socket is live, plus
    ``DISPLAY``/``XAUTHORITY`` when *x_display* is set and both its socket and the cookie
    file are live; at least one leg must be. Otherwise ``bridge_wrong_user`` when a
    socket exists but belongs to another uid, else ``bridge_unavailable``. *x11_dir* is
    a test seam (X clients always look in /tmp/.X11-unix).
    """
    env: dict[str, str] = {}
    states = [_socket_state(os.path.join(runtime_dir, SOURCE_WAYLAND_DISPLAY), uid)]
    if states[0] == _LIVE:
        env["WAYLAND_DISPLAY"] = SOURCE_WAYLAND_DISPLAY
        env["XDG_RUNTIME_DIR"] = runtime_dir
    if x_display is not None:
        states.append(_socket_state(os.path.join(x11_dir, f"X{x_display}"), uid))
        cookie = os.path.join(runtime_dir, BRIDGE_DIR, "xauth")
        if states[-1] == _LIVE and _cookie_ok(cookie, uid):
            env["DISPLAY"] = f":{x_display}"
            env["XAUTHORITY"] = cookie
    if env:
        return env
    return REASON_BRIDGE_WRONG_USER if _FOREIGN in states else REASON_BRIDGE_UNAVAILABLE
