"""What is the operator's desktop right now? (single source of truth)

The host may run a legacy Xorg (root-owned, grants SI:localuser) or a Wayland
compositor with a ROOTLESS Xwayland, which does NOT grant SI:localuser: a client
without the per-login, randomly-named cookie gets "Authorization required". Stale
cookie files and stale ``/tmp/.X11-unix/X<n>`` sockets linger, so neither a file's
existence nor a socket's means the desktop is up. This module finds the live X
server from ``/proc`` (never shelling out: callers run synchronously on an asyncio
loop), reads its cookie from argv/environ, and proves it with a real X11 handshake.

Pure and stateless: no module-level caches. Callers own any TTL caching.
"""
from __future__ import annotations

import logging
import os
import re
import socket
import stat
import struct
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

log = logging.getLogger("server.sessions.desktop_session")

DesktopKind = Literal["x11", "wayland"]

REASON_NO_GRAPHICAL_SESSION = "no_graphical_session"
REASON_COOKIE_MISSING = "cookie_missing"
REASON_CONNECT_REFUSED = "connect_refused"
REASON_INVALID_DISPLAY = "invalid_display"

_DISPLAY_RE = re.compile(r"^:(\d+)(?:\.\d+)?$")
_SERVER_ARG_RE = re.compile(r"^:\d+$")
_X_SERVERS = frozenset({"Xwayland", "Xorg", "X"})
_COOKIE_NAME = b"MIT-MAGIC-COOKIE-1"
_FAMILY_LOCAL = 0x0100
_FAMILY_WILD = 0xFFFF


@dataclass(frozen=True)
class DesktopSession:
    kind: DesktopKind
    display: str
    xauthority: str | None
    wayland_display: str | None
    xdg_runtime_dir: str | None

    def env(self, *, prefer_wayland: bool) -> dict[str, str]:
        out = {"DISPLAY": self.display}
        if self.xauthority:
            out["XAUTHORITY"] = self.xauthority
        if self.kind == "wayland" and prefer_wayland and self.wayland_display:
            out["WAYLAND_DISPLAY"] = self.wayland_display
            if self.xdg_runtime_dir:
                out["XDG_RUNTIME_DIR"] = self.xdg_runtime_dir
            out["XDG_SESSION_TYPE"] = "wayland"
        else:
            out["XDG_SESSION_TYPE"] = "x11"
        return out


@dataclass(frozen=True)
class DesktopUnavailable:
    reason: str
    detail: str = ""


@dataclass(frozen=True)
class _Server:
    number: int
    comm: str
    owned: bool
    pid_dir: str
    auth_arg: str | None


def _read(path: str) -> bytes | None:
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def _owner(pid_dir: str) -> int:
    """REAL uid of a process, from ``status``'s ``Uid:`` line (first field).

    Not the ``/proc/<pid>`` dir owner: a non-dumpable process (prctl PR_SET_DUMPABLE 0)
    shows that dir as root-owned, so any local user's fake ``Xorg`` would pass as root.
    Raises OSError when unreadable/unparseable (the caller skips the process).
    """
    raw = _read(os.path.join(pid_dir, "status"))
    if raw is None:
        raise OSError(f"no status for {pid_dir}")
    for line in raw.decode(errors="replace").splitlines():
        if line.startswith("Uid:"):
            try:
                return int(line.split()[1])
            except (IndexError, ValueError):
                break
    raise OSError(f"no Uid in status for {pid_dir}")


def socket_owner(path: str) -> int | None:
    """Owner uid of ``path`` iff it is a real socket (lstat: a symlink is rejected), else None."""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    return st.st_uid if stat.S_ISSOCK(st.st_mode) else None


def _environ(pid_dir: str) -> dict[str, str]:
    raw = _read(os.path.join(pid_dir, "environ")) or b""
    out: dict[str, str] = {}
    for item in raw.split(b"\0"):
        k, sep, v = item.decode(errors="replace").partition("=")
        if sep:
            out[k] = v
    return out


def _scan(proc_root: str, uid: int) -> list[_Server]:
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return []
    found: list[_Server] = []
    for name in entries:
        if not name.isdigit():
            continue
        pid_dir = os.path.join(proc_root, name)
        try:
            comm = (_read(os.path.join(pid_dir, "comm")) or b"").decode(errors="replace").strip()
            cmdline = _read(os.path.join(pid_dir, "cmdline"))
            if cmdline is None:
                continue
            argv = [a.decode(errors="replace") for a in cmdline.split(b"\0") if a]
            if not argv:
                continue
            exe = os.path.basename(argv[0])
            if comm not in _X_SERVERS and exe not in _X_SERVERS:
                continue
            number = next((int(a[1:]) for a in argv[1:] if _SERVER_ARG_RE.match(a)), None)
            if number is None:
                continue
            auth = next((argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "-auth"), None)
            owner = _owner(pid_dir)
        except OSError:
            continue  # raced with process exit / unreadable
        if owner not in (uid, 0):
            continue  # another user's process: its argv (-auth, :N) is not ours to trust
        owned = owner == uid
        found.append(_Server(number, comm if comm in _X_SERVERS else exe, owned, pid_dir, auth))
    return found


def resolve_desktop_session(
    *, uid: int, display: str | None = None, proc_root: str = "/proc",
    socket_dir: str = "/tmp/.X11-unix",
    probe: Callable[[str, str | None], bool] | None = None,
) -> DesktopSession | DesktopUnavailable:
    wanted: int | None = None
    if display is not None:
        m = _DISPLAY_RE.match(display.strip())
        if not m:
            return DesktopUnavailable(REASON_INVALID_DISPLAY, f"display {display!r} is not a local :N")
        wanted = int(m.group(1))

    servers = [
        s for s in _scan(proc_root, uid)
        if (wanted is None or s.number == wanted)
        and socket_owner(os.path.join(socket_dir, f"X{s.number}")) in (uid, 0)
    ]
    if not servers:
        return DesktopUnavailable(
            REASON_NO_GRAPHICAL_SESSION,
            f"no X server process for display :{wanted}" if wanted is not None else "no X server process")
    srv = min(servers, key=lambda s: (not s.owned, s.number))

    env = _environ(srv.pid_dir) if srv.owned else {}
    cookie = srv.auth_arg or env.get("XAUTHORITY") or None
    if cookie:
        try:
            os.lstat(cookie)
        except FileNotFoundError:
            return DesktopUnavailable(REASON_COOKIE_MISSING, cookie)
        except OSError:
            pass
        if _read(cookie) is None:
            cookie = None  # exists but unreadable (root-owned Xorg auth): legacy SI:localuser posture

    wayland_display = env.get("WAYLAND_DISPLAY") if srv.owned and srv.comm == "Xwayland" else None
    kind: DesktopKind = "wayland" if wayland_display else "x11"
    session = DesktopSession(
        kind=kind, display=f":{srv.number}", xauthority=cookie, wayland_display=wayland_display,
        xdg_runtime_dir=env.get("XDG_RUNTIME_DIR") if kind == "wayland" else None)

    sock = os.path.join(socket_dir, f"X{srv.number}")
    if not (probe or probe_x11)(sock, cookie):
        return DesktopUnavailable(REASON_CONNECT_REFUSED, sock)
    return session


def _cookie_for(xauthority: str | None, number: str) -> bytes:
    """MIT-MAGIC-COOKIE-1 for this display: a Local/Wild record wins over the first other-family match."""
    raw = _read(xauthority) if xauthority else None
    if not raw:
        return b""
    pos = 0
    first_any = b""
    try:
        while pos + 2 <= len(raw):
            (family,) = struct.unpack_from(">H", raw, pos)
            pos += 2
            fields: list[bytes] = []
            for _ in range(4):
                (n,) = struct.unpack_from(">H", raw, pos)
                pos += 2
                if pos + n > len(raw):
                    return first_any
                fields.append(raw[pos:pos + n])
                pos += n
            _addr, num, name, data = fields
            if name == _COOKIE_NAME and num in (b"", number.encode()):
                if family in (_FAMILY_LOCAL, _FAMILY_WILD):
                    return data
                first_any = first_any or data
    except struct.error:
        pass
    return first_any


def _pad4(b: bytes) -> bytes:
    return b + b"\0" * (-len(b) % 4)


def probe_x11(socket_path: str, xauthority: str | None, *, timeout_s: float = 0.5) -> bool:
    """True iff the X server on ``socket_path`` accepts a connection setup with our cookie."""
    m = re.search(r"X(\d+)$", socket_path)
    data = _cookie_for(xauthority, m.group(1) if m else "")
    name = _COOKIE_NAME if data else b""
    request = (struct.pack("<BxHHHHxx", 0x6C, 11, 0, len(name), len(data))
               + _pad4(name) + _pad4(data))
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(timeout_s)
        s.connect(socket_path)
        s.sendall(request)
        first = s.recv(1)
        return first == b"\x01"
    except OSError as e:
        log.debug("x11 probe of %s failed: %s", socket_path, e)
        return False
    finally:
        s.close()
