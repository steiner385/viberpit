"""The SOURCE host's side of the display bridge: is it live here, and what env points at it.

The target daemon's :mod:`server.sessions.display_bridge` forwards its desktop onto this
host over ssh (spec §8 "Verified invocation"). On the source that leaves, per source host
(one target at a time), everything inside the session user's private 0700 dirs:

* the waypipe server's Wayland socket ``$XDG_RUNTIME_DIR/wayland-viberpit-bridge``;
* the X11 forward ``$XDG_RUNTIME_DIR/viberpit-bridge/X11``;
* the target's cookie ``$XDG_RUNTIME_DIR/viberpit-bridge/xauth`` (0600).

The X11 leg deliberately avoids ``/tmp/.X11-unix``: with ``DISPLAY=:N`` libxcb connects to
the ABSTRACT socket ``@/tmp/.X11-unix/X<N>`` first, which any local user can bind (no
filesystem ownership applies), and session clients would hand the target's cookie to an
impostor. ``DISPLAY`` is the socket's absolute path instead — libxcb (≥1.13) then connects
to that filesystem path only, and authenticates as display 0 (verified on rocky, libxcb
1.17, with strace: one ``connect`` to the path, no abstract attempt).

This module owns that layout (the target side imports the names from here) and the one
liveness check: a socket counts only when it exists, is a socket, is owned by the
session user and accepts ``connect()``. The check blocks for up to ``_CONNECT_TIMEOUT_S``
per leg, so async callers run it in a worker thread.
"""
from __future__ import annotations

import os
import socket
import stat
from typing import Any

SOURCE_WAYLAND_DISPLAY = "wayland-viberpit-bridge"
BRIDGE_DIR = "viberpit-bridge"
X11_SOCKET = "X11"
XAUTH_FILE = "xauth"
# A path DISPLAY carries no display number; libxcb looks its cookie up as display 0.
X11_AUTH_DISPLAY = 0

# Fallback reasons (spec Global Constraints; exact strings).
REASON_BRIDGE_UNAVAILABLE = "bridge_unavailable"
REASON_BRIDGE_WRONG_USER = "bridge_wrong_user"
# Observed by the EDGE on the target (display_bridge_ensure state) and passed to this
# daemon as ``display_fallback_hint``; used only when the live check here also fails.
REASON_BRIDGE_NOT_ACCEPTED = "bridge_not_accepted"
REASON_TARGET_DESKTOP_UNAVAILABLE = "target_desktop_unavailable"
# Every reason a would-be-remote session went virtual for: a queued/held row carrying
# one re-resolves its remote leg at actual spawn (spec §2 "resolve at actual spawn").
BRIDGE_FALLBACK_REASONS: frozenset[str] = frozenset({
    REASON_BRIDGE_UNAVAILABLE, REASON_BRIDGE_WRONG_USER,
    REASON_BRIDGE_NOT_ACCEPTED, REASON_TARGET_DESKTOP_UNAVAILABLE,
})

_CONNECT_TIMEOUT_S = 0.5

_LIVE = "live"
_ABSENT = "absent"
_DEAD = "dead"
_FOREIGN = "foreign"


def bridge_x11_enabled(settings: Any) -> bool:
    """Whether this host runs (target) or exports (source) the bridge's X11 leg."""
    return bool(getattr(settings, "display_bridge_x11", True))


def source_runtime_dir(uid: int) -> str:
    """The session user's ``$XDG_RUNTIME_DIR`` (the daemon runs as that user)."""
    return os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{uid}"


def x11_socket_path(runtime_dir: str) -> str:
    """The X11 forward on the source; also the session's ``DISPLAY`` (path form)."""
    return os.path.join(runtime_dir, BRIDGE_DIR, X11_SOCKET)


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


def remote_env(*, runtime_dir: str, x11: bool, uid: int) -> dict[str, str] | str:
    """The env that points a session at the bridge, or why it can't.

    A live Wayland leg gives ``WAYLAND_DISPLAY``/``XDG_RUNTIME_DIR``; a live X11 leg (*x11*
    on, socket live, cookie file present) gives ``DISPLAY`` (the socket path) and
    ``XAUTHORITY``. A leg that is not live has its keys set to "" so an inherited value
    can never send a window to this host's own screen. At least one leg must be live;
    otherwise ``bridge_wrong_user`` when a socket exists but belongs to another uid, else
    ``bridge_unavailable``.
    """
    states = [_socket_state(os.path.join(runtime_dir, SOURCE_WAYLAND_DISPLAY), uid)]
    env = {"WAYLAND_DISPLAY": "", "DISPLAY": "", "XAUTHORITY": ""}
    live = False
    if states[0] == _LIVE:
        env["WAYLAND_DISPLAY"] = SOURCE_WAYLAND_DISPLAY
        env["XDG_RUNTIME_DIR"] = runtime_dir
        live = True
    if x11:
        sock = x11_socket_path(runtime_dir)
        states.append(_socket_state(sock, uid))
        cookie = os.path.join(runtime_dir, BRIDGE_DIR, XAUTH_FILE)
        if states[-1] == _LIVE and _cookie_ok(cookie, uid):
            env["DISPLAY"] = sock
            env["XAUTHORITY"] = cookie
            live = True
    if live:
        return env
    return REASON_BRIDGE_WRONG_USER if _FOREIGN in states else REASON_BRIDGE_UNAVAILABLE
