"""Dev servers a session started, found from ``/proc`` on the daemon that owns it (D-18).

Facts and rulings: ``docs/superpowers/specs/2026-11-05-d18-dev-server-preview-verification.md``
§B, and the D-18 plan's controller rulings. No psutil: the process table, the tree walk
and the session match are ``memory_watch``'s (``scan_proc``, ``_subtree``,
``match_session``) and ``adopt._read_proc``; this module only adds the socket half.

**Which listeners count** (a port is never entered by hand):

1. A LISTEN row of ``/proc/net/tcp`` or ``tcp6`` on port >= 1024, bound to loopback or to
   every address (``0.0.0.0`` / ``::``). A bind to one specific non-loopback address is
   not offered.
2. Owned by a process attributed to the session:
   - **subtree**: it descends from a direct child of the daemon (a *root*) that
     ``match_session`` names as this session — or names nobody (the Codex app-server is
     one per account, shared by every thread), in which case the process's OWN cwd must
     match the session. A root itself (the CLI, x11vnc, a proxy) is never a dev server,
     and neither is anything below a non-shell direct child of a root whose command line
     names MCP (the session's MCP servers).
   - **escaped**: outside every root (``setsid``/``nohup`` reparent to pid 1), it must be
     in the daemon's cgroup and its cwd must match the session.
   ``match_session``'s cwd rule is made strict here: when another live session has the
   same cwd and no ``--resume`` id decides, the process is unattributed.
3. Not a Chromium ``--remote-debugging-port`` (by command line).
4. Only then probed: ``GET /`` with ``Accept: text/html`` to the DETECTED address (``::1``
   stays ``::1``; ``0.0.0.0`` is reached over ``127.0.0.1``), answered with a non-error
   ``text/html`` within :data:`PROBE_TIMEOUT_S`. Nothing outside the session is probed.

Each entry: ``{port, address, bind: "loopback"|"all", pid, label, title?, note?}``.
``address`` is where the server is reached on its host; ``label`` is the background
task's command when the listener descends from that task's shell, else the listener's
command line; ``title`` is the page's ``<title>`` — untrusted text, capped.
``note`` (:data:`EXPOSED_NOTE`) marks a bind every interface can reach.

The edge adds the per-viewer ``open`` route (:func:`open_route`); it is never computed
here, because only the edge knows who is looking.
"""
from __future__ import annotations

import asyncio
import html
import ipaddress
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from server.sessions.adopt import ProcInfo, _read_proc
from server.sessions.display_policy import (PREVIEW_LOCAL, PREVIEW_TAILNET,
                                            resolve_preview_route)
from server.sessions.memory_watch import (ProcRow, SessionKey, _resume_arg, _subtree,
                                          match_session, scan_proc)

log = logging.getLogger(__name__)

MIN_PORT = 1024
PROBE_TIMEOUT_S = 1.0
#: Detection is polled while the Browser tab is open; a few seconds of cache keeps that
#: to one ``/proc`` pass and one probe round per session however many viewers poll.
CACHE_TTL_S = 3.0
EXPOSED_NOTE = "Also reachable from your network"

DEV_SERVER_NOT_DETECTED = "DEV_SERVER_NOT_DETECTED"
PREVIEW_SESSION_NOT_LIVE = "PREVIEW_SESSION_NOT_LIVE"
PREVIEW_NO_DISPLAY = "PREVIEW_NO_DISPLAY"
PREVIEW_LAUNCH_FAILED = "PREVIEW_LAUNCH_FAILED"
#: Every refusal code; the remote edge rebuilds only these.
DEV_SERVER_CODES = frozenset({DEV_SERVER_NOT_DETECTED, PREVIEW_SESSION_NOT_LIVE,
                              PREVIEW_NO_DISPLAY, PREVIEW_LAUNCH_FAILED})

_LISTEN = "0A"
_SHELLS = frozenset({"bash", "sh", "dash", "zsh", "fish", "ksh"})
_MCP = re.compile(r"mcp|modelcontextprotocol", re.IGNORECASE)
_DEBUG_PORT = "--remote-debugging-port"
_LABEL_MAX = 160
_TITLE_MAX = 200
_BODY_MAX = 64 * 1024
_TITLE = re.compile(rb"<title[^>]*>(.*?)</title", re.IGNORECASE | re.DOTALL)

_Address = ipaddress.IPv4Address | ipaddress.IPv6Address


class DevServerError(Exception):
    """A preview open refused. ``error_data`` rides the daemon's RPC envelope, so a remote
    edge rebuilds it with its code (``RemoteOrchestrator.open_dev_server``), and marks it
    an expected refusal the daemon logs at INFO."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code
        self.error_data = {"code": code}


@dataclass(frozen=True)
class Listener:
    inode: int
    address: _Address
    port: int


@dataclass(frozen=True)
class ProbeResult:
    title: str | None


def _decode_address(hexaddr: str) -> _Address:
    raw = bytes.fromhex(hexaddr)
    if len(raw) == 4:
        return ipaddress.IPv4Address(raw[::-1])
    # tcp6: four host-order (little-endian) 32-bit words.
    return ipaddress.IPv6Address(b"".join(raw[i:i + 4][::-1] for i in range(0, 16, 4)))


def parse_net_tcp(text: str) -> list[Listener]:
    """The LISTEN rows of one ``/proc/net/tcp{,6}`` table."""
    out: list[Listener] = []
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 10 or fields[3] != _LISTEN:
            continue
        try:
            hexaddr, hexport = fields[1].split(":")
            out.append(Listener(int(fields[9]), _decode_address(hexaddr), int(hexport, 16)))
        except ValueError:
            continue
    return out


class ProcFs:
    """The ``/proc`` reads detection needs, rooted at ``root`` so tests can hand it a
    ``/proc``-shaped tree. The process table and per-pid reads are memory_watch's and
    adopt's; the socket table, ``fd`` links and cgroup are the only new reads."""

    def __init__(self, root: str = "/proc") -> None:
        self.root = root

    def scan(self) -> list[ProcRow]:
        return scan_proc(self.root)

    def describe(self, pid: int) -> ProcInfo | None:
        return _read_proc(pid, self.root)

    def listeners(self) -> dict[int, Listener]:
        out: dict[int, Listener] = {}
        for table in ("tcp", "tcp6"):
            try:
                with open(f"{self.root}/net/{table}", encoding="ascii", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                continue
            out.update((lst.inode, lst) for lst in parse_net_tcp(text))
        return out

    def socket_inodes(self, pid: int) -> set[int]:
        base = f"{self.root}/{pid}/fd"
        try:
            names = os.listdir(base)
        except OSError:
            return set()
        out: set[int] = set()
        for name in names:
            try:
                target = os.readlink(f"{base}/{name}")
            except OSError:
                continue  # closed mid-walk
            if target.startswith("socket:[") and target.endswith("]"):
                try:
                    out.add(int(target[8:-1]))
                except ValueError:
                    continue
        return out

    def cgroup(self, pid: int) -> str | None:
        try:
            with open(f"{self.root}/{pid}/cgroup", encoding="utf-8", errors="replace") as fh:
                return fh.read().strip()
        except OSError:
            return None


@dataclass(frozen=True)
class Candidate:
    """An attributed listener, before the probe."""
    port: int
    address: str          # where to connect (the detected address; 0.0.0.0 → 127.0.0.1)
    bind: str             # "loopback" | "all"
    pid: int
    cmdline: tuple[str, ...]
    shells: tuple[str, ...]  # the command lines of tool shells above the listener


def _bind_kind(addr: _Address) -> str | None:
    mapped = getattr(addr, "ipv4_mapped", None)
    addr = mapped or addr
    if addr.is_loopback:
        return "loopback"
    if addr.is_unspecified:
        return "all"
    return None  # one specific non-loopback address: not offered


def _connect_address(addr: _Address) -> str:
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped
    if addr.is_unspecified:
        return "127.0.0.1" if addr.version == 4 else "::1"
    return str(addr)


def _exe(info: ProcInfo | None, row: ProcRow | None) -> str:
    """The program name: argv[0]'s basename (``-bash`` → ``bash``), else ``comm``."""
    if info is not None and info.cmdline:
        return os.path.basename(info.cmdline[0]).lstrip("-")
    return row.comm if row is not None else ""


def session_of(info: ProcInfo | None, sessions: list[SessionKey]) -> str | None:
    """``match_session``, refusing a cwd match another session shares (no ``--resume``
    id decides it): a dev server is offered to exactly one session or to none."""
    sid = match_session(info, sessions)
    if sid is None or info is None:
        return None
    resume = _resume_arg(info.cmdline)
    if resume and any(s.session_id == sid and s.engine_session_id == resume for s in sessions):
        return sid
    winner = next(s for s in sessions if s.session_id == sid)
    cwd = winner.cwd.rstrip("/")
    if any(s.session_id != sid and s.cwd and s.cwd.rstrip("/") == cwd for s in sessions):
        return None
    return sid


def find_candidates(session_id: str, sessions: list[SessionKey], *, fs: ProcFs,
                    daemon_pid: int) -> list[Candidate]:
    """Every listener attributed to ``session_id`` by the module rules 1–3. Reads only;
    probes nothing."""
    listeners = {ino: lst for ino, lst in fs.listeners().items()
                 if lst.port >= MIN_PORT and _bind_kind(lst.address) is not None}
    if not listeners or not any(s.session_id == session_id for s in sessions):
        return []
    rows = fs.scan()
    by_pid = {r.pid: r for r in rows}
    children: dict[int, list[ProcRow]] = {}
    for r in rows:
        children.setdefault(r.ppid, []).append(r)
    root_of: dict[int, int] = {}
    for top in children.get(daemon_pid, ()):
        for member in _subtree(top.pid, children, by_pid):
            root_of[member.pid] = top.pid
    infos: dict[int, ProcInfo | None] = {}
    owners: dict[int, str | None] = {}

    def info(pid: int) -> ProcInfo | None:
        if pid not in infos:
            infos[pid] = fs.describe(pid)
        return infos[pid]

    def owner(pid: int) -> str | None:
        if pid not in owners:
            owners[pid] = session_of(info(pid), sessions)
        return owners[pid]

    daemon_cgroup = fs.cgroup(daemon_pid)
    # pid -> the tool shells' command lines above it (subtree), or () (escaped).
    eligible: dict[int, tuple[str, ...]] = {}
    depth: dict[int, int] = {}
    for row in rows:
        pid = row.pid
        if pid == daemon_pid:
            continue
        root = root_of.get(pid)
        if root is None:
            if daemon_cgroup is None or fs.cgroup(pid) != daemon_cgroup:
                continue
            if owner(pid) != session_id:
                continue
            eligible[pid], depth[pid] = (), 0
            continue
        if pid == root:
            continue  # the agent itself, or the daemon's own helper
        root_owner = owner(root)
        if root_owner is not None and root_owner != session_id:
            continue
        chain = [pid]
        while chain[-1] != root:
            parent = by_pid.get(chain[-1])
            if parent is None:
                break
            chain.append(parent.ppid)
        if chain[-1] != root:
            continue
        chain.reverse()  # root … pid
        branch = chain[1]
        branch_info = info(branch)
        if _exe(branch_info, by_pid.get(branch)) not in _SHELLS and branch_info is not None \
                and _MCP.search(" ".join(branch_info.cmdline)):
            continue  # one of the session's MCP servers
        if root_owner is None and owner(pid) != session_id:
            continue  # a shared root (Codex app-server): the process's own cwd decides
        shells = []
        for p in chain[1:]:
            pi = info(p)
            if pi is not None and _exe(pi, by_pid.get(p)) in _SHELLS:
                shells.append(" ".join(pi.cmdline))
        eligible[pid], depth[pid] = tuple(shells), len(chain)

    holders: dict[int, int] = {}  # inode -> the outermost eligible pid holding it
    for pid in eligible:
        for ino in fs.socket_inodes(pid):
            if ino in listeners and (ino not in holders or depth[pid] < depth[holders[ino]]):
                holders[ino] = pid
    by_port: dict[int, Candidate] = {}
    for ino, pid in holders.items():
        pi = info(pid)
        cmdline = tuple(pi.cmdline) if pi is not None else ()
        if any(arg.startswith(_DEBUG_PORT) for arg in cmdline):
            continue
        lst = listeners[ino]
        bind = _bind_kind(lst.address)
        assert bind is not None
        cand = Candidate(lst.port, _connect_address(lst.address), bind, pid, cmdline,
                         eligible[pid])
        prev = by_port.get(lst.port)
        if prev is None:
            by_port[lst.port] = cand
        else:
            # One server on both families (``localhost`` → 127.0.0.1 and ::1): one entry,
            # IPv4 preferred, exposed if either bind is.
            v4 = cand if ":" not in cand.address else prev
            by_port[lst.port] = Candidate(
                lst.port, v4.address, "all" if "all" in (prev.bind, cand.bind) else "loopback",
                v4.pid, v4.cmdline, v4.shells)
    return [by_port[p] for p in sorted(by_port)]


def _host_literal(address: str) -> str:
    return f"[{address}]" if ":" in address else address


async def probe_html(address: str, port: int, *, timeout: float = PROBE_TIMEOUT_S
                     ) -> ProbeResult | None:
    """``GET /`` with ``Accept: text/html``. A :class:`ProbeResult` when the server answers
    a non-error ``text/html`` within ``timeout``, else None — never raises an OSError or
    a timeout."""
    writer = None
    try:
        async with asyncio.timeout(timeout):
            reader, writer = await asyncio.open_connection(address, port)
            writer.write((f"GET / HTTP/1.1\r\nHost: {_host_literal(address)}:{port}\r\n"
                          "Accept: text/html\r\nUser-Agent: viberpit-dev-server-probe\r\n"
                          "Connection: close\r\n\r\n").encode("ascii"))
            await writer.drain()
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            parts = lines[0].split()
            if len(parts) < 2 or not parts[0].startswith("HTTP/") or not parts[1].isdigit() \
                    or int(parts[1]) >= 400:
                return None
            ctype = next((ln.split(":", 1)[1].strip().lower() for ln in lines[1:]
                          if ln.lower().startswith("content-type:")), "")
            if not ctype.startswith("text/html"):
                return None
            body = b""
            while len(body) < _BODY_MAX and not _TITLE.search(body):
                chunk = await reader.read(_BODY_MAX - len(body))
                if not chunk:
                    break
                body += chunk
    except (OSError, TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError,
            UnicodeError, ValueError):
        return None
    finally:
        if writer is not None:
            writer.close()
    return ProbeResult(title=_title(body))


def _title(body: bytes) -> str | None:
    m = _TITLE.search(body)
    if m is None:
        return None
    text = html.unescape(m.group(1).decode("utf-8", "replace"))
    text = " ".join("".join(c if c.isprintable() else " " for c in text).split())
    return text[:_TITLE_MAX] or None


def _label(cand: Candidate, bg_commands: list[str]) -> str:
    for cmd in bg_commands:
        quoted = cmd.replace("'", "'\\''")
        if cmd and any(cmd in sh or quoted in sh for sh in cand.shells):
            return cmd[:_LABEL_MAX]
    return " ".join(cand.cmdline)[:_LABEL_MAX] or str(cand.pid)


def server_url(server: dict) -> str:
    """The URL that reaches a detected server on its own host: ``http://<address>:<port>/``
    from detected data only."""
    return f"http://{_host_literal(server['address'])}:{int(server['port'])}/"


def open_route(server: dict, *, client_node: str | None, runner: str,
               tailnet_name: str | None) -> dict:
    """The ``open`` object for one entry, for this viewer (``display_policy`` decides)."""
    kind = resolve_preview_route(client_node=client_node, runner=runner,
                                 exposed=server.get("bind") == "all",
                                 tailnet_reachable=bool(tailnet_name))
    port = int(server["port"])
    if kind == PREVIEW_LOCAL:
        host = "[::1]" if ":" in server["address"] else "localhost"
        return {"kind": kind, "url": f"http://{host}:{port}/"}
    if kind == PREVIEW_TAILNET:
        assert tailnet_name is not None
        return {"kind": kind, "url": f"http://{_host_literal(tailnet_name)}:{port}/",
                "note": EXPOSED_NOTE}
    return {"kind": kind}


class DevServerDetector:
    """Per-daemon detection with a short per-session cache. ``fs``, ``daemon_pid``,
    ``probe`` and ``clock`` are injectable for tests."""

    def __init__(self, *, fs: ProcFs | None = None, daemon_pid: int | None = None,
                 probe: Callable[[str, int], Awaitable[ProbeResult | None]] | None = None,
                 clock: Callable[[], float] = time.monotonic, ttl: float = CACHE_TTL_S) -> None:
        self._fs = fs or ProcFs()
        self._daemon_pid = daemon_pid if daemon_pid is not None else os.getpid()
        self._probe = probe or probe_html
        self._clock = clock
        self._ttl = ttl
        self._cache: dict[str, tuple[float, list[dict]]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def detect(self, session_id: str, sessions: list[SessionKey], *,
                     bg_commands: Callable[[], Awaitable[list[str]]],
                     fresh: bool = False) -> list[dict]:
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            now = self._clock()
            for sid, (at, _) in list(self._cache.items()):
                if now - at >= self._ttl:
                    del self._cache[sid]
                    stale = self._locks.get(sid)
                    if sid != session_id and stale is not None and not stale.locked():
                        self._locks.pop(sid, None)
            hit = self._cache.get(session_id)
            if hit is not None and not fresh:
                return [dict(s) for s in hit[1]]
            servers = await self._detect(session_id, sessions, bg_commands)
            self._cache[session_id] = (self._clock(), servers)
            return [dict(s) for s in servers]

    async def _detect(self, session_id: str, sessions: list[SessionKey],
                      bg_commands: Callable[[], Awaitable[list[str]]]) -> list[dict]:
        cands = await asyncio.to_thread(find_candidates, session_id, sessions,
                                        fs=self._fs, daemon_pid=self._daemon_pid)
        if not cands:
            return []
        probes = await asyncio.gather(*(self._probe(c.address, c.port) for c in cands))
        found = [(c, p) for c, p in zip(cands, probes) if p is not None]
        if not found:
            return []
        cmds = await bg_commands()
        out: list[dict] = []
        for cand, probe in found:
            entry: dict[str, Any] = {"port": cand.port, "address": cand.address,
                                     "bind": cand.bind, "pid": cand.pid,
                                     "label": _label(cand, cmds)}
            if probe.title:
                entry["title"] = probe.title
            if cand.bind == "all":
                entry["note"] = EXPOSED_NOTE
            out.append(entry)
        return out
