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
   - **subtree**: it descends from a direct child of the daemon (a *root*) that is this
     session by ``--resume`` id or an EXACT cwd (:func:`root_session_of`) — or that is
     nobody's (the Codex app-server is one per account, shared by every thread; a
     daemon helper), in which case the process's OWN cwd must be inside the session's
     MANAGED worktree (:func:`worktree_session_of`). A process whose own cwd matches
     ANOTHER session is never claimed, whatever its root. A root itself (the CLI, x11vnc, a proxy) is never a dev server,
     and neither is anything below a non-shell direct child of a root whose command line
     names MCP (the session's MCP servers).
   - **escaped**: outside every root (``setsid``/``nohup`` reparent to pid 1), it must be
     in the daemon's cgroup and its cwd inside the session's MANAGED worktree.
   A session whose cwd is not exactly a managed worktree root (``~``, a plain checkout,
   a ``<clone>`` grouping dir) claims only its own subtree. Session cwds are compared
   by realpath; process cwds as the kernel reports them (already canonical).
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
import threading
import time
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from typing import Any

from viberpit import worktrees

from server.auth.identity import _self_tailscale_ips
from server.sessions.adopt import ProcInfo, _read_proc
from server.sessions.display_policy import (PREVIEW_LOCAL, PREVIEW_NONE, PREVIEW_TAILNET,
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
#: The /proc pass takes ~20 ms; anything near this is a blocked read, not a slow host.
SCAN_TIMEOUT_S = 5.0
#: While a scan is stuck, polls answer with the last good list; past this age it is
#: dropped, so dead servers cannot stay listed (their Open would only refuse).
LAST_GOOD_MAX_AGE_S = 30.0
_SCAN_THREAD = "dev-servers-scan"

DEV_SERVER_NOT_DETECTED = "DEV_SERVER_NOT_DETECTED"
#: ``VIBERPIT_DEV_SERVER_DETECT_ENABLED`` is off on the owning host.
DEV_SERVER_DETECT_DISABLED = "DEV_SERVER_DETECT_DISABLED"
PREVIEW_SESSION_NOT_LIVE = "PREVIEW_SESSION_NOT_LIVE"
PREVIEW_NO_DISPLAY = "PREVIEW_NO_DISPLAY"
PREVIEW_LAUNCH_FAILED = "PREVIEW_LAUNCH_FAILED"
#: Every refusal code; the remote edge rebuilds only these.
DEV_SERVER_CODES = frozenset({DEV_SERVER_NOT_DETECTED, DEV_SERVER_DETECT_DISABLED,
                              PREVIEW_SESSION_NOT_LIVE, PREVIEW_NO_DISPLAY,
                              PREVIEW_LAUNCH_FAILED})
#: ``open.reason`` of a ``none`` route: the owning host has no X11 for a ``vnc`` preview.
NO_ROUTE_NO_DISPLAY = "host_no_display"

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


def _realpath(path: str | None) -> str:
    return os.path.realpath(path) if path else ""


def _inside(path: str, base: str) -> bool:
    return path == base or path.startswith(base.rstrip("/") + "/")


def managed_worktree_roots(sessions: list[SessionKey], repo_roots: Collection[str]
                           ) -> frozenset[str]:
    """The realpath cwds of the *sessions* that are exactly a managed session worktree
    root (``worktrees.session_worktree_root``) under one of *repo_roots*. Stats every
    session cwd, so it runs inside the bounded scan (:class:`DevServerDetector`)."""
    out: set[str] = set()
    for k in sessions:
        if not k.cwd:
            continue
        real = os.path.realpath(k.cwd)
        if any(str(worktrees.session_worktree_root(k.cwd, r) or "") == real
               for r in repo_roots):
            out.add(real)
    return frozenset(out)


def managed_sessions(sessions: list[SessionKey], managed_worktrees: frozenset[str]
                     ) -> list[SessionKey]:
    """The sessions whose (realpath) cwd IS a managed worktree root —
    ``managed_worktrees``, from ``worktrees.session_worktree_root`` on the daemon. Only
    these may claim a process outside their own subtree; ``~``, a plain checkout, a
    scratch dir or a ``<clone>`` grouping dir above the worktrees never do."""
    return [s for s in sessions if s.cwd and s.cwd in managed_worktrees]


def worktree_session_of(cwd: str | None, managed: list[SessionKey]) -> str | None:
    """The managed session whose worktree contains ``cwd`` (realpath), deepest first;
    None when none does or two sessions share the winning worktree."""
    if not cwd:
        return None
    hits = [s for s in managed if _inside(cwd, s.cwd)]
    if not hits:
        return None
    deepest = max(len(s.cwd) for s in hits)
    best = [s for s in hits if len(s.cwd) == deepest]
    return best[0].session_id if len(best) == 1 else None


def root_session_of(info: ProcInfo | None, sessions: list[SessionKey]) -> str | None:
    """The session a daemon child (a *root*) is, trusted only on strong evidence: its
    ``--resume`` id, or a cwd EXACTLY one live session's. Never ``match_session``'s
    prefix rule alone: a session at ``~`` would otherwise own every root below it — the
    per-account Codex app-server (cwd = the Codex home) and the daemon's helpers — and
    with them every other session's dev servers."""
    if info is None:
        return None
    resume = _resume_arg(info.cmdline)
    if resume:
        hits = [s.session_id for s in sessions if s.engine_session_id == resume]
        if len(hits) == 1:
            return hits[0]
    if info.cwd:
        cwd = info.cwd.rstrip("/") or "/"
        hits = [s.session_id for s in sessions if s.cwd and (s.cwd.rstrip("/") or "/") == cwd]
        if len(hits) == 1:
            return hits[0]
    return None


def find_candidates(session_id: str, sessions: list[SessionKey], *, fs: ProcFs,
                    daemon_pid: int, managed_worktrees: frozenset[str] = frozenset()
                    ) -> list[Candidate]:
    """Every listener attributed to ``session_id`` by the module rules 1–3. Reads only;
    probes nothing."""
    listeners = {ino: lst for ino, lst in fs.listeners().items()
                 if lst.port >= MIN_PORT and _bind_kind(lst.address) is not None}
    if not listeners or not any(s.session_id == session_id for s in sessions):
        return []
    # A session's recorded cwd may be a symlink: resolve it. A process cwd is NOT
    # resolved — the kernel's /proc/<pid>/cwd link is already canonical, and a realpath
    # would stat every component (a stalled FUSE mount would hang the scan).
    sessions = [SessionKey(s.session_id, s.engine_session_id, _realpath(s.cwd))
                for s in sessions]
    managed = managed_sessions(sessions, managed_worktrees)
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

    def managed_owner(pid: int) -> str | None:
        pi = info(pid)
        return worktree_session_of(pi.cwd if pi is not None else None, managed)

    def owner(pid: int) -> str | None:
        if pid not in owners:
            owners[pid] = session_of(info(pid), sessions)
        return owners[pid]

    root_owners: dict[int, str | None] = {}

    def root_owner_of(pid: int) -> str | None:
        if pid not in root_owners:
            root_owners[pid] = root_session_of(info(pid), sessions)
        return root_owners[pid]

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
            if managed_owner(pid) != session_id:
                continue  # escaped: only a cwd inside the session's managed worktree
            eligible[pid], depth[pid] = (), 0
            continue
        if pid == root:
            continue  # the agent itself, or the daemon's own helper
        root_owner = root_owner_of(root)
        if root_owner is not None and root_owner != session_id:
            continue
        own = owner(pid)
        if own is not None and own != session_id:
            continue  # another session's, whatever tree it sits in
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
        if root_owner is None and managed_owner(pid) != session_id:
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


def self_tailnet_ip() -> str | None:
    """This host's own tailnet address (IPv4 preferred), from the ``tailscale status``
    read identity already makes and caches — or None off-tailnet. Reported by the
    daemon that owns the session, so every host (a unix-socket dobby included) names
    its own. Blocking (a cached subprocess): call it off the event loop."""
    ips = sorted(_self_tailscale_ips(), key=lambda a: (a.version, int(a)))
    return str(ips[0]) if ips else None


def _ip_or_none(value: object) -> str | None:
    try:
        return str(ipaddress.ip_address(str(value))) if value else None
    except ValueError:
        return None


def open_route(server: dict, *, client_node: str | None, runner: str,
               tailnet_ip: str | None, x11: bool = True) -> dict:
    """The ``open`` object for one entry, for this viewer (``display_policy`` decides).

    ``local-link`` is the detected address itself (``127.0.0.2`` is not ``localhost``).
    ``tailnet-link`` is the host's tailnet IP, never its MagicDNS name: Vite >= 6
    refuses an unlisted ``Host`` name but accepts an IP literal. No valid IP → ``vnc``,
    or ``{kind: "none", reason}`` when the owning host reports no X11 (``x11``)."""
    ip = _ip_or_none(tailnet_ip)
    kind = resolve_preview_route(client_node=client_node, runner=runner,
                                 exposed=server.get("bind") == "all",
                                 tailnet_reachable=ip is not None, vnc_available=x11)
    if kind == PREVIEW_NONE:
        return {"kind": kind, "reason": NO_ROUTE_NO_DISPLAY}
    port = int(server["port"])
    if kind == PREVIEW_LOCAL:
        return {"kind": kind, "url": server_url(server)}
    if kind == PREVIEW_TAILNET:
        assert ip is not None
        return {"kind": kind, "url": f"http://{_host_literal(ip)}:{port}/",
                "note": EXPOSED_NOTE}
    return {"kind": kind}


class DevServerDetector:
    """Per-daemon detection with a short per-session cache. ``fs``, ``daemon_pid``,
    ``probe`` and ``clock`` are injectable for tests.

    **One scan at a time per host.** The ``/proc`` pass and the session-cwd
    ``realpath``s run on ONE dedicated daemon thread, never the loop's shared default
    executor, bounded by ``scan_timeout``. A read blocked on a stalled mount cannot be
    cancelled, so while that thread is still running no second one is started: every
    poll answers at once with the session's last good result (``[]`` for a fresh
    detection, which an Open must never trust stale). One stuck mount therefore costs
    one thread, not one per poll."""

    def __init__(self, *, fs: ProcFs | None = None, daemon_pid: int | None = None,
                 probe: Callable[[str, int], Awaitable[ProbeResult | None]] | None = None,
                 clock: Callable[[], float] = time.monotonic, ttl: float = CACHE_TTL_S,
                 scan_timeout: float = SCAN_TIMEOUT_S) -> None:
        self._scan_timeout = scan_timeout
        self._fs = fs or ProcFs()
        self._daemon_pid = daemon_pid if daemon_pid is not None else os.getpid()
        self._probe = probe or probe_html
        self._clock = clock
        self._ttl = ttl
        self._cache: dict[str, tuple[float, list[dict]]] = {}
        self._last_good: dict[str, tuple[float, list[dict]]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._scan_lock = asyncio.Lock()
        self._scan_future: asyncio.Future[list[Candidate]] | None = None

    async def detect(self, session_id: str, sessions: list[SessionKey], *,
                     bg_commands: Callable[[], Awaitable[list[str]]],
                     fresh: bool = False,
                     managed_worktrees: frozenset[str] = frozenset(),
                     repo_roots: Collection[str] = ()) -> list[dict]:
        """``managed_worktrees`` are already-resolved worktree roots; ``repo_roots`` are
        resolved to more of them (:func:`managed_worktree_roots`) inside the scan."""
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            now = self._clock()
            for sid, (at, _) in list(self._cache.items()):
                if now - at >= self._ttl:
                    del self._cache[sid]
                    stale = self._locks.get(sid)
                    if sid != session_id and stale is not None and not stale.locked():
                        self._locks.pop(sid, None)
            live = {s.session_id for s in sessions}
            for sid in [sid for sid in self._last_good if sid not in live]:
                del self._last_good[sid]
            hit = self._cache.get(session_id)
            if hit is not None and not fresh:
                return [dict(s) for s in hit[1]]
            servers = await self._detect(session_id, sessions, bg_commands,
                                         managed_worktrees, tuple(repo_roots), fresh=fresh)
            self._cache[session_id] = (self._clock(), servers)
            return [dict(s) for s in servers]

    def _start_scan(self, fn: Callable[[], list[Candidate]]) -> asyncio.Future[list[Candidate]]:
        """Run *fn* on a new daemon thread (a blocked one never holds up process exit)
        and resolve the returned future on this loop."""
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[list[Candidate]] = loop.create_future()
        # A scan that outlives its waiter may still fail: mark it retrieved.
        fut.add_done_callback(lambda f: f.cancelled() or f.exception())

        def settle(result: list[Candidate] | None, exc: BaseException | None) -> None:
            if fut.done():
                return
            if exc is not None:
                fut.set_exception(exc)
            else:
                fut.set_result(result or [])

        def run() -> None:
            result: list[Candidate] | None = None
            exc: BaseException | None = None
            try:
                result = fn()
            except Exception as e:  # noqa: BLE001 — handed to the awaiting poll
                exc = e
            try:
                loop.call_soon_threadsafe(settle, result, exc)
            except RuntimeError:
                pass  # the loop closed while this read was blocked: nobody is waiting
        threading.Thread(target=run, name=_SCAN_THREAD, daemon=True).start()
        return fut

    async def _scan(self, session_id: str, fn: Callable[[], list[Candidate]]
                    ) -> list[Candidate] | None:
        """The candidates, or None when the host's scan is stuck (this poll timed out,
        or an earlier scan's thread is still blocked)."""
        async with self._scan_lock:
            fut = self._scan_future
            if fut is not None and not fut.done():
                return None  # never a second thread while one is blocked
            fut = self._scan_future = self._start_scan(fn)
            try:
                return await asyncio.wait_for(asyncio.shield(fut), self._scan_timeout)
            except TimeoutError:
                log.info("dev_servers.scan_timeout session=%s after=%.1fs", session_id,
                         self._scan_timeout)
                return None

    async def _detect(self, session_id: str, sessions: list[SessionKey],
                      bg_commands: Callable[[], Awaitable[list[str]]],
                      managed_worktrees: frozenset[str], repo_roots: tuple[str, ...], *,
                      fresh: bool) -> list[dict]:
        def scan() -> list[Candidate]:
            managed = managed_worktrees
            if repo_roots:
                managed = managed | managed_worktree_roots(sessions, repo_roots)
            return find_candidates(session_id, sessions, fs=self._fs,
                                   daemon_pid=self._daemon_pid, managed_worktrees=managed)
        cands = await self._scan(session_id, scan)
        if cands is None:
            if fresh:
                return []
            at, good = self._last_good.get(session_id, (0.0, []))
            if self._clock() - at > LAST_GOOD_MAX_AGE_S:
                self._last_good.pop(session_id, None)
                return []
            return [dict(s) for s in good]
        out = await self._entries(cands, bg_commands)
        self._last_good[session_id] = (self._clock(), out)
        return out

    async def _entries(self, cands: list[Candidate],
                       bg_commands: Callable[[], Awaitable[list[str]]]) -> list[dict]:
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
