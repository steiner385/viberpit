"""D-18 dev-server detection and per-viewer preview routing, server side.

- Detection (``server/sessions/dev_servers.py``) runs on the daemon that owns the
  session, from ``/proc`` only: LISTEN sockets from ``net/tcp{,6}``, mapped to pids
  through ``fd/``, attributed to the session by its process subtree (or, outside every
  subtree, the daemon's cgroup plus a ``match_session`` cwd hit), port >= 1024, not an
  MCP server or a Chromium debug port, and only then probed: ``GET /`` must answer
  ``text/html`` within 1 s.
- ``GET /api/sessions/{id}/dev-servers`` adds an ``open`` route per entry from the
  REQUESTING viewer's verified tailnet node: ``local-link`` on the session's own host,
  ``tailnet-link`` for a ``0.0.0.0`` bind, else ``vnc``.
- ``POST …/dev-servers/{port}/open`` re-detects, refuses an undetected port, starts the
  session's virtual display on demand (even for a native session) and opens the
  detected URL in it. Person-only, audited ``session.preview_open``.
"""
from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from unittest.mock import patch

import pytest

from server.ipc.client import DaemonClient, DaemonError
from server.ipc.daemon import create_daemon_app
from server.ipc.orchestrator_rpc import register_orchestrator_rpc
from server.sessions import dev_servers as ds
from server.sessions.display_policy import DISPLAY_NATIVE, DISPLAY_VIRTUAL
from server.sessions.engine import AgentEngine
from server.sessions.facade import InProcessOrchestratorAdapter
from server.sessions.fleet_orchestrator import _MOVE_BLOCKED, _PER_SESSION, FleetOrchestrator
from server.sessions.memory_watch import SessionKey
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.remote import RemoteOrchestrator
from server.sessions.types import SessionRecord
from server.tests.ipc_harness import serve_daemon_uds
from server.tests.test_sessions_actions_api import _ENV, _H, _audit_events, _make_client

DAEMON_CG = "0::/system.slice/viberpit-daemon.service\n"
SID_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SID_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
SID_C = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
SID = "11111111-1111-4111-8111-111111111111"  # the route tests' fake record


# ---------------------------------------------------------------- a /proc-shaped tree


def _hex4(addr: str) -> str:
    return ipaddress.IPv4Address(addr).packed[::-1].hex().upper()


def _hex6(addr: str) -> str:
    raw = ipaddress.IPv6Address(addr).packed
    return "".join(raw[i:i + 4][::-1].hex().upper() for i in range(0, 16, 4))


_NET_HEADER = ("  sl  local_address rem_address   st tx_queue rx_queue tr tm->when "
               "retrnsmt   uid  timeout inode\n")


class FakeProc:
    """Writes just the ``/proc`` files detection reads: ``<pid>/{stat,cmdline,cwd,
    cgroup,fd/*}`` and ``net/tcp{,6}``."""

    def __init__(self, root: Path) -> None:
        self.root = root
        (root / "net").mkdir(parents=True)
        self._net: dict[str, list[str]] = {"tcp": [], "tcp6": []}

    def add(self, pid: int, ppid: int, cmdline: list[str], *, cwd: str = "/",
            cgroup: str = DAEMON_CG, sockets: tuple[int, ...] = ()) -> None:
        d = self.root / str(pid)
        (d / "fd").mkdir(parents=True)
        comm = os.path.basename(cmdline[0])[:15]
        (d / "stat").write_text(f"{pid} ({comm}) S {ppid} " + " ".join(["0"] * 30) + "\n")
        (d / "cmdline").write_bytes(b"\0".join(c.encode() for c in cmdline) + b"\0")
        (d / "cwd").symlink_to(cwd)
        (d / "cgroup").write_text(cgroup)
        for i, ino in enumerate(sockets, start=3):
            (d / "fd" / str(i)).symlink_to(f"socket:[{ino}]")
        (d / "fd" / "0").symlink_to("/dev/null")

    def listen(self, inode: int, addr: str, port: int, *, state: str = "0A") -> None:
        v6 = ":" in addr
        local = f"{_hex6(addr) if v6 else _hex4(addr)}:{port:04X}"
        rem = ("0" * 32 if v6 else "00000000") + ":0000"
        n = len(self._net["tcp6" if v6 else "tcp"])
        self._net["tcp6" if v6 else "tcp"].append(
            f"{n:4d}: {local} {rem} {state} 00000000:00000000 00:00000000 00000000  1000"
            f"        0 {inode} 1 0000000000000000 100 0 0 10 0\n")
        for name, lines in self._net.items():
            (self.root / "net" / name).write_text(_NET_HEADER + "".join(lines))


def _keys(*pairs: tuple[str, str, str]) -> list[SessionKey]:
    return [SessionKey(sid, esid, cwd) for sid, esid, cwd in pairs]


class _Probe:
    """Records every address a probe was sent to; answers HTML on ``html`` ports."""

    def __init__(self, html: dict[int, str | None]) -> None:
        self.html = html
        self.calls: list[tuple[str, int]] = []

    async def __call__(self, address: str, port: int) -> ds.ProbeResult | None:
        self.calls.append((address, port))
        if port in self.html:
            return ds.ProbeResult(title=self.html[port])
        return None


@pytest.fixture
def host(tmp_path: Path) -> SimpleNamespace:
    """One host: daemon 100 with sessions A (Claude, /w/a), B (Claude, /w/b) and C
    (Codex, /w/c, under the account's shared app-server), plus everything that must
    never be listed for A."""
    p = FakeProc(tmp_path / "proc")
    # The daemon, with its own :9443 listener — never a session's.
    p.add(100, 1, ["/usr/bin/python3", "-m", "server.ipc"], cwd="/srv/viberpit",
          sockets=(9040,))
    p.listen(9040, "0.0.0.0", 9443)
    # Session A: claude → bash (tool shell) → python (dev server, 127.0.0.1:5173)
    p.add(200, 100, ["claude", "--resume", "esid-a"], cwd="/w/a")
    p.add(201, 200, ["/bin/bash", "-c", "eval 'cd site && python -m http.server 5173'"],
          cwd="/w/a")
    p.add(202, 201, ["python", "-m", "http.server", "5173"], cwd="/w/a", sockets=(9001,))
    # A second tool shell of A's, parent of the listeners below.
    p.add(210, 200, ["/bin/bash", "-c", "eval 'npx vite --host'"], cwd="/w/a")
    p.listen(9001, "127.0.0.1", 5173)
    # A's MCP server (a non-shell child of the CLI) answering HTML on 4000: excluded.
    p.add(203, 200, ["npx", "-y", "chrome-devtools-mcp@latest"], cwd="/w/a", sockets=(9002,))
    p.listen(9002, "127.0.0.1", 4000)
    # A privileged port in A's tree: excluded.
    p.add(204, 210, ["python", "-m", "http.server", "80"], cwd="/w/a", sockets=(9007,))
    p.listen(9007, "127.0.0.1", 80)
    # A Chromium debug port in A's tree: excluded.
    p.add(205, 210, ["chrome", "--remote-debugging-port=9222"], cwd="/w/a", sockets=(9008,))
    p.listen(9008, "127.0.0.1", 9222)
    # A 0.0.0.0 bind and an IPv6-only bind in A's tree: listed.
    p.add(206, 210, ["node", "vite", "--host"], cwd="/w/a", sockets=(9010,))
    p.listen(9010, "0.0.0.0", 8080)
    p.add(207, 210, ["node", "next", "dev"], cwd="/w/a/web", sockets=(9011,))
    p.listen(9011, "::1", 5180)
    # A raw socket in A's tree (accepts, never answers HTML): probed, not listed.
    p.add(208, 210, ["python", "raw.py"], cwd="/w/a", sockets=(9012,))
    p.listen(9012, "127.0.0.1", 5199)
    # A non-LISTEN socket of A's dev server: ignored.
    p.listen(9013, "127.0.0.1", 5174, state="01")
    # Session B: its own dev server, on 5300.
    p.add(300, 100, ["claude", "--resume", "esid-b"], cwd="/w/b")
    p.add(301, 300, ["/bin/bash", "-c", "npm run dev"], cwd="/w/b")
    p.add(302, 301, ["node", "vite"], cwd="/w/b", sockets=(9003,))
    p.listen(9003, "127.0.0.1", 5300)
    # Session C (Codex): the account's app-server is shared, so its children are
    # attributed by their own cwd.
    p.add(600, 100, ["/usr/bin/codex", "app-server"], cwd="/home/u/.codex-home")
    p.add(601, 600, ["bash", "-lc", "npm run dev"], cwd="/w/c")
    p.add(602, 601, ["node", "vite"], cwd="/w/c", sockets=(9020,))
    p.listen(9020, "127.0.0.1", 5400)
    # A Codex child whose cwd is A's worktree is A's (the root names nobody).
    p.add(603, 600, ["bash", "-lc", "pnpm dev"], cwd="/w/a/app")
    p.add(604, 603, ["node", "vite"], cwd="/w/a/app", sockets=(9021,))
    p.listen(9021, "127.0.0.1", 5401)
    # The daemon's own infrastructure: x11vnc is a direct child (a root), even with
    # A's cwd — never a dev server.
    p.add(110, 100, ["x11vnc", "-rfbport", "5901"], cwd="/w/a", sockets=(9030,))
    p.listen(9030, "127.0.0.1", 5901)
    # Postgres on the host: another cgroup, not in any subtree.
    p.add(400, 1, ["postgres"], cwd="/var/lib/postgresql",
          cgroup="0::/system.slice/postgresql.service\n", sockets=(9004,))
    p.listen(9004, "127.0.0.1", 5432)
    # The VM's VNC, outside the daemon, in A's cwd even: other cgroup, never listed.
    p.add(410, 1, ["qemu"], cwd="/w/a", cgroup="0::/machine.slice/win11.scope\n",
          sockets=(9005,))
    p.listen(9005, "127.0.0.1", 5900)
    # A detached (setsid) server A started: reparented to 1, still in the daemon's
    # cgroup, cwd inside A's worktree → A's.
    p.add(500, 1, ["node", "server.js"], cwd="/w/a/api", sockets=(9006,))
    p.listen(9006, "127.0.0.1", 3000)
    # A detached process with no worktree cwd: unattributed, never probed.
    p.add(501, 1, ["node", "other.js"], cwd="/tmp", sockets=(9009,))
    p.listen(9009, "127.0.0.1", 3001)
    keys = _keys((SID_A, "esid-a", "/w/a"), (SID_B, "esid-b", "/w/b"), (SID_C, "", "/w/c"))
    fs = ds.ProcFs(str(p.root))
    probe = _Probe({5173: "Directory listing", 8080: "Vite App", 5180: None, 3000: None,
                    5401: None, 4000: "mcp", 5300: "B", 5400: "C", 5432: None, 5900: None,
                    3001: None, 5901: None, 9222: None, 80: None, 9443: None})
    return SimpleNamespace(proc=p, fs=fs, keys=keys, probe=probe)


def _detector(host, **kw) -> ds.DevServerDetector:
    return ds.DevServerDetector(fs=host.fs, daemon_pid=100, probe=host.probe, **kw)


async def _no_bg() -> list[str]:
    return []


# ---------------------------------------------------------------- parsing


def test_net_tcp_keeps_only_listen_rows_and_decodes_both_families(host) -> None:
    listeners = host.fs.listeners()
    assert listeners[9001] == ds.Listener(9001, ipaddress.ip_address("127.0.0.1"), 5173)
    assert listeners[9011] == ds.Listener(9011, ipaddress.ip_address("::1"), 5180)
    assert listeners[9010].address == ipaddress.ip_address("0.0.0.0")
    assert 9013 not in listeners  # ESTABLISHED, not LISTEN


def test_socket_inodes_read_fd_links(host) -> None:
    assert host.fs.socket_inodes(202) == {9001}
    assert host.fs.socket_inodes(99999) == set()


# ---------------------------------------------------------------- attribution (RF 1)


@pytest.mark.asyncio
async def test_detects_only_the_sessions_servers_and_probes_nothing_outside(host) -> None:
    servers = await _detector(host).detect(SID_A, host.keys, bg_commands=_no_bg)
    by_port = {s["port"]: s for s in servers}
    assert sorted(by_port) == [3000, 5173, 5180, 5401, 8080]
    probed = {port for _, port in host.probe.calls}
    # Review Focus 1: B's server, C's server, Postgres, the VM's VNC, x11vnc, the MCP
    # server, the debug port, port 80 and the unattributed detached process are never
    # even probed.
    assert probed == {3000, 5173, 5180, 5401, 8080, 5199}
    assert not probed & {5300, 5400, 5432, 5900, 5901, 4000, 9222, 80, 3001, 9443}


@pytest.mark.asyncio
async def test_entry_shape_bind_and_detected_connect_address(host) -> None:
    servers = {s["port"]: s for s in
               await _detector(host).detect(SID_A, host.keys, bg_commands=_no_bg)}
    assert servers[5173] == {"port": 5173, "address": "127.0.0.1", "bind": "loopback",
                             "pid": 202, "label": "python -m http.server 5173",
                             "title": "Directory listing"}
    # 0.0.0.0 is reached over loopback, flagged as exposed.
    assert servers[8080]["address"] == "127.0.0.1" and servers[8080]["bind"] == "all"
    assert servers[8080]["note"] == ds.EXPOSED_NOTE
    # An IPv6-only bind is probed and opened at its own address, never 127.0.0.1.
    assert servers[5180]["address"] == "::1" and "title" not in servers[5180]
    assert ("::1", 5180) in host.probe.calls


@pytest.mark.asyncio
async def test_other_sessions_see_their_own_servers(host) -> None:
    b = await _detector(host).detect(SID_B, host.keys, bg_commands=_no_bg)
    c = await _detector(host).detect(SID_C, host.keys, bg_commands=_no_bg)
    assert [s["port"] for s in b] == [5300]
    # Codex: found under the shared app-server by the child's own cwd.
    assert [s["port"] for s in c] == [5400]


@pytest.mark.asyncio
async def test_two_sessions_sharing_a_cwd_attribute_nothing_by_cwd(host) -> None:
    keys = host.keys + _keys(("dddddddd-dddd-4ddd-8ddd-dddddddddddd", "", "/w/a"))
    servers = await _detector(host).detect(SID_A, keys, bg_commands=_no_bg)
    ports = sorted(s["port"] for s in servers)
    # A's own CLI still matches by --resume; the cwd-only matches (the detached server
    # and the Codex child) are ambiguous and dropped.
    assert ports == [5173, 5180, 8080]


@pytest.mark.asyncio
async def test_unknown_session_lists_nothing_and_probes_nothing(host) -> None:
    servers = await _detector(host).detect("nope", host.keys, bg_commands=_no_bg)
    assert servers == [] and host.probe.calls == []


# ---------------------------------------------------------------- labels, cache


@pytest.mark.asyncio
async def test_label_prefers_the_background_task_command(host) -> None:
    async def bg() -> list[str]:
        return ["cd site && python -m http.server 5173", "unrelated"]
    servers = {s["port"]: s for s in await _detector(host).detect(SID_A, host.keys, bg_commands=bg)}
    assert servers[5173]["label"] == "cd site && python -m http.server 5173"
    assert servers[8080]["label"] == "node vite --host"


@pytest.mark.asyncio
async def test_results_are_cached_briefly_and_fresh_rescans(host) -> None:
    now = [1000.0]
    det = _detector(host, clock=lambda: now[0])
    await det.detect(SID_A, host.keys, bg_commands=_no_bg)
    n = len(host.probe.calls)
    await det.detect(SID_A, host.keys, bg_commands=_no_bg)
    assert len(host.probe.calls) == n  # cached
    await det.detect(SID_A, host.keys, bg_commands=_no_bg, fresh=True)
    assert len(host.probe.calls) == 2 * n
    now[0] += ds.CACHE_TTL_S + 0.1
    await det.detect(SID_A, host.keys, bg_commands=_no_bg)
    assert len(host.probe.calls) == 3 * n


# ---------------------------------------------------------------- the probe


@pytest.mark.asyncio
async def test_probe_reads_html_and_title_and_refuses_non_html() -> None:
    async def serve(reader, writer, body: bytes, ctype: bytes) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: " + ctype + b"\r\nContent-Length: "
                     + str(len(body)).encode() + b"\r\n\r\n" + body)
        await writer.drain()
        writer.close()

    html = await asyncio.start_server(
        lambda r, w: serve(r, w, b"<html><title> My &amp; App\n</title></html>",
                           b"text/html; charset=utf-8"), "127.0.0.1", 0)
    js = await asyncio.start_server(
        lambda r, w: serve(r, w, b"{}", b"application/json"), "127.0.0.1", 0)
    async with html, js:
        hp = html.sockets[0].getsockname()[1]
        jp = js.sockets[0].getsockname()[1]
        assert await ds.probe_html("127.0.0.1", hp) == ds.ProbeResult(title="My & App")
        assert await ds.probe_html("127.0.0.1", jp) is None
    with socket.socket() as s:  # nothing listening: refused, never raises
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
    assert await ds.probe_html("127.0.0.1", free) is None


@pytest.mark.asyncio
async def test_probe_gives_up_on_a_silent_listener_within_the_timeout() -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        t0 = time.monotonic()
        assert await ds.probe_html("127.0.0.1", s.getsockname()[1], timeout=0.2) is None
        assert time.monotonic() - t0 < 1.0


# ---------------------------------------------------------------- the real /proc


@pytest.mark.slow
@pytest.mark.asyncio
async def test_real_proc_detects_http_server_and_not_a_raw_socket(tmp_path: Path) -> None:
    """No-model contract test: a stand-in session root (a direct child of this process,
    cwd in its worktree) starts ``python -m http.server`` on 127.0.0.1 and a raw
    listener. The real ``/proc`` scan finds the server and never lists the raw socket."""
    work = tmp_path / "worktree"
    work.mkdir()
    (work / "index.html").write_text("<title>stand-in</title>")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    raw = ("import socket,sys,time\ns=socket.socket();s.bind(('127.0.0.1',0));s.listen()\n"
           "print(s.getsockname()[1],flush=True)\ntime.sleep(60)\n")
    (work / "raw.py").write_text(raw)
    script = (f"{sys.executable} -m http.server {port} --bind 127.0.0.1 >/dev/null 2>&1 & "
              f"{sys.executable} raw.py & wait")
    root = subprocess.Popen(["bash", "-c", script], cwd=work, stdout=subprocess.PIPE,
                            text=True, start_new_session=True)
    try:
        assert root.stdout is not None
        raw_port = int(await asyncio.wait_for(asyncio.to_thread(root.stdout.readline), 10))
        keys = [SessionKey(SID_A, "", str(work))]
        det = ds.DevServerDetector(daemon_pid=os.getpid())
        servers: list[dict] = []
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            servers = await det.detect(SID_A, keys, bg_commands=_no_bg, fresh=True)
            if any(s["port"] == port for s in servers):
                break
            await asyncio.sleep(0.1)
        ports = {s["port"]: s for s in servers}
        assert port in ports, servers
        assert ports[port]["address"] == "127.0.0.1" and ports[port]["bind"] == "loopback"
        assert ports[port]["title"] == "stand-in"
        assert raw_port not in ports
        # Another session (another worktree) sees none of it.
        other = await det.detect(SID_B, keys + [SessionKey(SID_B, "", str(tmp_path / "b"))],
                                 bg_commands=_no_bg, fresh=True)
        assert other == []
    finally:
        os.killpg(root.pid, 15)
        root.wait(timeout=10)


# ---------------------------------------------------------------- routing (RF 3)


_LOOP = {"port": 5173, "address": "127.0.0.1", "bind": "loopback", "pid": 1, "label": "x"}
_V6 = {"port": 5180, "address": "::1", "bind": "loopback", "pid": 1, "label": "x"}
_ALL = {"port": 8080, "address": "127.0.0.1", "bind": "all", "pid": 1, "label": "x",
        "note": ds.EXPOSED_NOTE}


@pytest.mark.parametrize("server,client_node,tailnet,expected", [
    (_LOOP, "rocky", "rocky.tail.ts.net", {"kind": "local-link", "url": "http://localhost:5173/"}),
    (_LOOP, "ROCKY", None, {"kind": "local-link", "url": "http://localhost:5173/"}),
    (_V6, "rocky", None, {"kind": "local-link", "url": "http://[::1]:5180/"}),
    (_ALL, "rocky", "rocky.tail.ts.net", {"kind": "local-link", "url": "http://localhost:8080/"}),
    (_LOOP, "phone", "rocky.tail.ts.net", {"kind": "vnc"}),
    (_LOOP, None, "rocky.tail.ts.net", {"kind": "vnc"}),
    (_ALL, "phone", "rocky.tail.ts.net",
     {"kind": "tailnet-link", "url": "http://rocky.tail.ts.net:8080/", "note": ds.EXPOSED_NOTE}),
    (_ALL, "phone", None, {"kind": "vnc"}),
    (_ALL, None, "100.64.0.9",
     {"kind": "tailnet-link", "url": "http://100.64.0.9:8080/", "note": ds.EXPOSED_NOTE}),
])
def test_open_route(server, client_node, tailnet, expected) -> None:
    assert ds.open_route(server, client_node=client_node, runner="rocky",
                         tailnet_name=tailnet) == expected


def test_server_url_brackets_ipv6() -> None:
    assert ds.server_url(_LOOP) == "http://127.0.0.1:5173/"
    assert ds.server_url(_V6) == "http://[::1]:5180/"


# ---------------------------------------------------------------- the orchestrator


class _FakeX11:
    def __init__(self, *, start_ok: bool = True, launch_ok: bool = True) -> None:
        self.start_ok, self.launch_ok = start_ok, launch_ok
        self.started: list[str] = []
        self.stopped: list[str] = []
        self.launched: list[tuple[str, str | None]] = []
        self._specs: dict[str, object] = {}

    def display_for(self, sid):
        return self._specs.get(sid)

    async def start_display(self, sid):
        self.started.append(sid)
        if not self.start_ok:
            return None
        self._specs.setdefault(sid, object())
        return self._specs[sid]

    async def stop_display(self, sid):
        self.stopped.append(sid)
        self._specs.pop(sid, None)

    async def launch_browser(self, sid, url):
        self.launched.append((sid, url))
        return self.launch_ok


def _rec(sid: str, cwd: str, *, display_mode: str = DISPLAY_NATIVE) -> SessionRecord:
    rec = SessionRecord(id=sid, name="n", account="a", engine=AgentEngine.CLAUDE_CODE,
                        engine_session_id="esid", cols=80, rows=24, cwd=cwd, detached=False)
    rec.display_mode = display_mode
    return rec


@pytest.fixture
def orch():
    o = SessionOrchestrator(sdk_factory=mock.MagicMock(), poll_interval=9999.0,
                            usage_fn=lambda name: None)
    o._records[SID_A] = _rec(SID_A, "/w/a")
    o._drivers[SID_A] = mock.MagicMock()
    o._x11 = _FakeX11()
    found = [{"port": 5173, "address": "127.0.0.1", "bind": "loopback", "pid": 202,
              "label": "vite"}]
    calls: list[tuple] = []

    async def detect(session_id, sessions, *, bg_commands, fresh=False):
        calls.append((session_id, [k.session_id for k in sessions], fresh))
        return [dict(s) for s in found]
    o._dev_server_detector().detect = detect  # type: ignore[method-assign]
    return SimpleNamespace(o=o, found=found, calls=calls)


@pytest.mark.asyncio
async def test_orchestrator_dev_servers_names_this_host(orch) -> None:
    with patch("server.sessions.orchestrator.self_host_name", return_value="rocky"):
        res = await orch.o.dev_servers(SID_A)
    assert res == {"host": "rocky", "servers": orch.found}
    assert orch.calls == [(SID_A, [SID_A], False)]
    with pytest.raises(KeyError):
        await orch.o.dev_servers("nope")


@pytest.mark.asyncio
async def test_dev_servers_kill_switch(orch, monkeypatch) -> None:
    monkeypatch.setenv("VIBERPIT_DEV_SERVER_DETECT_ENABLED", "false")
    res = await orch.o.dev_servers(SID_A)
    assert res["servers"] == [] and orch.calls == []


@pytest.mark.asyncio
async def test_open_starts_a_display_on_demand_for_a_native_session(orch) -> None:
    res = await orch.o.open_dev_server(SID_A, port=5173)
    x11 = orch.o._x11
    assert res == {"ok": True, "port": 5173, "url": "http://127.0.0.1:5173/"}
    assert orch.calls[-1][2] is True  # re-detected, never served from the cache
    assert x11.started == [SID_A] and x11.launched == [(SID_A, "http://127.0.0.1:5173/")]
    assert orch.o._records[SID_A].display_mode == DISPLAY_NATIVE  # unchanged
    # The preview display is reaped with the session's existing display lifecycle.
    await orch.o._teardown_driver_state(SID_A)
    assert x11.stopped == [SID_A] and x11.display_for(SID_A) is None


@pytest.mark.asyncio
async def test_open_uses_the_detected_ipv6_address(orch) -> None:
    orch.found[:] = [{"port": 5180, "address": "::1", "bind": "loopback", "pid": 1, "label": "x"}]
    res = await orch.o.open_dev_server(SID_A, port=5180)
    assert res["url"] == "http://[::1]:5180/"
    assert orch.o._x11.launched == [(SID_A, "http://[::1]:5180/")]


@pytest.mark.asyncio
async def test_open_refuses_a_port_that_is_not_detected_now(orch) -> None:
    """Review Focus 2: the URL can't be steered — only a currently detected port opens."""
    with pytest.raises(ds.DevServerError) as ei:
        await orch.o.open_dev_server(SID_A, port=5432)
    assert ei.value.code == ds.DEV_SERVER_NOT_DETECTED
    assert orch.o._x11.started == [] and orch.o._x11.launched == []


@pytest.mark.asyncio
async def test_open_refuses_without_a_live_driver_and_starts_nothing(orch) -> None:
    del orch.o._drivers[SID_A]
    with pytest.raises(ds.DevServerError) as ei:
        await orch.o.open_dev_server(SID_A, port=5173)
    assert ei.value.code == ds.PREVIEW_SESSION_NOT_LIVE
    assert orch.o._x11.started == []


@pytest.mark.asyncio
async def test_open_reaps_a_display_whose_driver_went_away_meanwhile(orch) -> None:
    x11 = orch.o._x11
    real_start = x11.start_display

    async def start_then_teardown(sid):
        spec = await real_start(sid)
        orch.o._drivers.pop(sid, None)  # torn down while the display started
        return spec
    x11.start_display = start_then_teardown
    with pytest.raises(ds.DevServerError) as ei:
        await orch.o.open_dev_server(SID_A, port=5173)
    assert ei.value.code == ds.PREVIEW_SESSION_NOT_LIVE
    assert x11.stopped == [SID_A] and x11.launched == []


@pytest.mark.asyncio
@pytest.mark.parametrize("x11,code", [
    (None, ds.PREVIEW_NO_DISPLAY),
    (_FakeX11(start_ok=False), ds.PREVIEW_NO_DISPLAY),
    (_FakeX11(launch_ok=False), ds.PREVIEW_LAUNCH_FAILED),
])
async def test_open_display_and_launch_failures(orch, x11, code) -> None:
    orch.o._x11 = x11
    with pytest.raises(ds.DevServerError) as ei:
        await orch.o.open_dev_server(SID_A, port=5173)
    assert ei.value.code == code


@pytest.mark.asyncio
async def test_open_on_a_virtual_session_reuses_its_display(orch) -> None:
    orch.o._records[SID_A].display_mode = DISPLAY_VIRTUAL
    x11 = orch.o._x11
    await x11.start_display(SID_A)  # started at spawn
    await orch.o.open_dev_server(SID_A, port=5173)
    assert x11.launched == [(SID_A, "http://127.0.0.1:5173/")]
    assert len(x11._specs) == 1


# ---------------------------------------------------------------- RPC / remote / fleet (RF 4)


class _RecordingClient:
    def __init__(self, result=None, error: Exception | None = None):
        self.result, self.error = result, error
        self.calls: list[tuple[str, dict]] = []

    async def call(self, method, *, timeout=None, **kwargs):
        self.calls.append((method, kwargs))
        if self.error is not None:
            raise self.error
        return self.result


@pytest.mark.asyncio
async def test_remote_forwards_and_rebuilds_errors() -> None:
    client = _RecordingClient(result={"host": "rocky", "servers": []})
    ro = RemoteOrchestrator(client)
    assert await ro.dev_servers(SID_A) == {"host": "rocky", "servers": []}
    await ro.open_dev_server(SID_A, port=5173)
    assert client.calls == [("dev_servers", {"session_id": SID_A}),
                            ("open_dev_server", {"session_id": SID_A, "port": 5173})]
    with pytest.raises(KeyError):
        await RemoteOrchestrator(_RecordingClient(
            error=DaemonError("KeyError", "nope"))).dev_servers("nope")
    with pytest.raises(ds.DevServerError) as ei:
        await RemoteOrchestrator(_RecordingClient(error=DaemonError(
            "DevServerError", ds.DEV_SERVER_NOT_DETECTED,
            {"code": ds.DEV_SERVER_NOT_DETECTED}))).open_dev_server(SID_A, port=1)
    assert ei.value.code == ds.DEV_SERVER_NOT_DETECTED
    with pytest.raises(DaemonError):  # an unknown code is never invented
        await RemoteOrchestrator(_RecordingClient(error=DaemonError(
            "DevServerError", "WAT", {"code": "WAT"}))).open_dev_server(SID_A, port=1)
    with pytest.raises(DaemonError) as old:
        await RemoteOrchestrator(_RecordingClient(
            error=DaemonError("UnknownMethod", "dev_servers"))).dev_servers(SID_A)
    assert old.value.err_type == "UnknownMethod"


@pytest.mark.asyncio
async def test_uds_round_trip_runs_on_the_owner_daemon(orch) -> None:
    app = create_daemon_app()
    register_orchestrator_rpc(app, orch.o)
    async with serve_daemon_uds(app) as sock:
        client = DaemonClient(sock)
        try:
            ro = RemoteOrchestrator(client)
            listed = await ro.dev_servers(SID_A)
            opened = await ro.open_dev_server(SID_A, port=5173)
            with pytest.raises(ds.DevServerError) as refused:
                await ro.open_dev_server(SID_A, port=6000)
            with pytest.raises(KeyError):
                await ro.dev_servers("nope")
        finally:
            await client.aclose()
    assert listed["servers"] == orch.found
    assert opened["url"] == "http://127.0.0.1:5173/"
    assert refused.value.code == ds.DEV_SERVER_NOT_DETECTED
    assert orch.o._x11.launched == [(SID_A, "http://127.0.0.1:5173/")]


@pytest.mark.asyncio
async def test_fleet_runs_detection_and_open_on_the_owning_host() -> None:
    """Review Focus 4: a rocky session's detection and VNC open run on rocky."""
    assert {"dev_servers", "open_dev_server"} <= _PER_SESSION
    assert "open_dev_server" in _MOVE_BLOCKED and "dev_servers" not in _MOVE_BLOCKED

    class _HostOrch:
        def __init__(self, name):
            self.name, self.calls = name, []

        async def dev_servers(self, session_id):
            self.calls.append(("dev_servers", session_id))
            return {"host": self.name, "servers": []}

        async def open_dev_server(self, session_id, *, port):
            self.calls.append(("open_dev_server", session_id, port))
            return {"ok": True, "host": self.name}

    class _Router:
        def __init__(self, orchs):
            self._orchs = orchs

        def get(self, name):
            return self._orchs[name]

        def names(self):
            return sorted(self._orchs)

    dobby, rocky = _HostOrch("dobby"), _HostOrch("rocky")
    fo = FleetOrchestrator(_Router({"dobby": dobby, "rocky": rocky}), primary="dobby")  # type: ignore[arg-type]
    fo._host_cache[SID_A] = "rocky"
    assert (await fo.dev_servers(SID_A))["host"] == "rocky"
    assert (await fo.open_dev_server(SID_A, port=5173))["host"] == "rocky"
    assert rocky.calls == [("dev_servers", SID_A), ("open_dev_server", SID_A, 5173)]
    assert dobby.calls == []
    adapter = InProcessOrchestratorAdapter(dobby)  # type: ignore[arg-type]
    assert (await adapter.dev_servers(SID_A))["host"] == "dobby"
    assert (await adapter.open_dev_server(SID_A, port=1))["host"] == "dobby"


# ---------------------------------------------------------------- the routes


_DETECTED = [dict(_LOOP), dict(_ALL)]


def _route_client(*, host_name: str = "rocky", error: Exception | None = None,
                  open_error: Exception | None = None):
    client, fake = _make_client()
    fake.dev_calls = []

    async def dev_servers(sid):
        fake.dev_calls.append(("dev_servers", sid))
        if error is not None:
            raise error
        return {"host": host_name, "servers": [dict(s) for s in _DETECTED]}

    async def open_dev_server(sid, *, port):
        fake.dev_calls.append(("open_dev_server", sid, port))
        if open_error is not None:
            raise open_error
        return {"ok": True, "port": port, "url": f"http://127.0.0.1:{port}/"}
    fake.dev_servers = dev_servers
    fake.open_dev_server = open_dev_server
    return client, fake


_NODE = "server.api.sessions.client_node_name"
_TAILNET = "server.api.sessions.host_tailnet_name"


@pytest.mark.parametrize("viewer,tailnet,kinds", [
    ("rocky", "rocky.tail.ts.net", ["local-link", "local-link"]),
    ("dobby", "rocky.tail.ts.net", ["vnc", "tailnet-link"]),
    ("phone", None, ["vnc", "vnc"]),
    (None, "rocky.tail.ts.net", ["vnc", "tailnet-link"]),
])
def test_get_routes_open_per_viewer(viewer, tailnet, kinds) -> None:
    client, fake = _route_client()

    async def tailnet_name(name):
        assert name == "rocky"
        return tailnet
    with patch.dict(os.environ, _ENV), patch(_NODE, return_value=viewer), \
            patch(_TAILNET, side_effect=tailnet_name), client:
        r = client.get(f"/api/sessions/{SID}/dev-servers", headers=_H)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["host"] == "rocky"
    assert [s["open"]["kind"] for s in body["servers"]] == kinds
    assert fake.dev_calls == [("dev_servers", SID)]


def test_get_from_the_session_host_links_to_localhost() -> None:
    client, _ = _route_client()
    with patch.dict(os.environ, _ENV), patch(_NODE, return_value="rocky"), client:
        body = client.get(f"/api/sessions/{SID}/dev-servers", headers=_H).json()
    assert body["servers"][0]["open"] == {"kind": "local-link", "url": "http://localhost:5173/"}


def test_locality_cannot_be_spoofed_by_headers_or_params(monkeypatch) -> None:
    """Review Focus 3: the viewer's node comes only from ``tailscale whois`` of the
    verified peer. Whois names the session's host only for the tailnet address an
    attacker would claim; neither a header nor a param naming the host, nor that
    address in a forwarded header, makes the viewer local."""
    from server.auth import identity
    claimed = "100.64.0.7"

    def fake_run(argv, **kw):
        if argv[:2] == ["tailscale", "whois"] and argv[-1] == claimed:
            return SimpleNamespace(returncode=0, stdout='{"Node": {"ComputedName": "rocky"}}')
        return SimpleNamespace(returncode=1, stdout="")
    monkeypatch.setattr(identity.subprocess, "run", fake_run)
    client, _ = _route_client()
    with patch.dict(os.environ, _ENV), patch(_TAILNET, return_value=None), client:
        r = client.get(f"/api/sessions/{SID}/dev-servers", params={"node": "rocky"},
                       headers={**_H, "X-Viberpit-Client-Node": "rocky", "Host": "rocky"})
        # X-Forwarded-For is the client's own choice (only tailscaled may set it), so
        # the claimed address is never asked about. (X-Real-IP is honoured only from a
        # trusted front end that overwrites it — identity's one peer-trust rule, the
        # same one the display router's session-create path relies on.)
        forwarded = client.get(f"/api/sessions/{SID}/dev-servers",
                               headers={**_H, "X-Forwarded-For": claimed})
    assert r.status_code == 200
    assert [s["open"]["kind"] for s in r.json()["servers"]] == ["vnc", "vnc"]
    assert forwarded.status_code == 401 or all(
        s["open"]["kind"] != "local-link" for s in forwarded.json()["servers"])


def test_get_with_no_servers_skips_the_whois() -> None:
    client, fake = _route_client()

    async def none(sid):
        return {"host": "rocky", "servers": []}
    fake.dev_servers = none
    with patch.dict(os.environ, _ENV), patch(_NODE) as node, client:
        r = client.get(f"/api/sessions/{SID}/dev-servers", headers=_H)
    assert r.json() == {"host": "rocky", "servers": []}
    node.assert_not_called()


@pytest.mark.parametrize("error,status,code", [
    (KeyError(SID), 404, None),
    (DaemonError("UnknownMethod", "dev_servers"), 409, "daemon_outdated"),
])
def test_get_maps_errors(error, status, code) -> None:
    client, _ = _route_client(error=error)
    with patch.dict(os.environ, _ENV), client:
        r = client.get(f"/api/sessions/{SID}/dev-servers", headers=_H)
        anon = client.get(f"/api/sessions/{SID}/dev-servers")
    assert r.status_code == status
    if code:
        assert r.json()["detail"]["code"] == code
    assert anon.status_code == 401


def test_post_open_runs_on_the_owner_and_audits() -> None:
    client, fake = _route_client()
    with patch.dict(os.environ, _ENV), client:
        r = client.post(f"/api/sessions/{SID}/dev-servers/5173/open", headers=_H)
        events = client.portal.call(_audit_events)
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "port": 5173, "url": "http://127.0.0.1:5173/"}
    assert fake.dev_calls == [("open_dev_server", SID, 5173)]
    assert [e[2] for e in events if e[0] == "session.preview_open"] == [
        {"sessionId": SID, "port": 5173, "url": "http://127.0.0.1:5173/"}]


def test_post_open_is_person_only(monkeypatch) -> None:
    from server.auth import identity
    monkeypatch.setattr(identity, "attested_front_end_hop", lambda _conn: False)
    client, fake = _route_client()
    with patch.dict(os.environ, _ENV), client:
        r = client.post(f"/api/sessions/{SID}/dev-servers/5173/open", headers=_H)
        events = client.portal.call(_audit_events)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "SESSION_CONTROL_HUMAN_ONLY"
    assert fake.dev_calls == [] and not [e for e in events if e[0] == "session.preview_open"]


@pytest.mark.parametrize("error,status,code", [
    (ds.DevServerError(ds.DEV_SERVER_NOT_DETECTED), 404, ds.DEV_SERVER_NOT_DETECTED),
    (ds.DevServerError(ds.PREVIEW_SESSION_NOT_LIVE), 409, ds.PREVIEW_SESSION_NOT_LIVE),
    (ds.DevServerError(ds.PREVIEW_NO_DISPLAY), 409, ds.PREVIEW_NO_DISPLAY),
    (ds.DevServerError(ds.PREVIEW_LAUNCH_FAILED), 409, ds.PREVIEW_LAUNCH_FAILED),
    (DaemonError("UnknownMethod", "open_dev_server"), 409, "daemon_outdated"),
    (KeyError(SID), 404, None),
])
def test_post_open_maps_refusals_at_info(error, status, code, caplog) -> None:
    import logging
    caplog.set_level(logging.INFO)
    client, _ = _route_client(open_error=error)
    with patch.dict(os.environ, _ENV), client:
        r = client.post(f"/api/sessions/{SID}/dev-servers/5173/open", headers=_H)
        events = client.portal.call(_audit_events)
    assert r.status_code == status
    if code:
        assert r.json()["detail"]["code"] == code
    assert not [e for e in events if e[0] == "session.preview_open"]
    assert not [rec for rec in caplog.records
                if rec.levelno >= logging.WARNING and "preview" in rec.getMessage()]


def test_post_open_rejects_a_non_port_path() -> None:
    client, fake = _route_client()
    with patch.dict(os.environ, _ENV), client:
        bad = [client.post(f"/api/sessions/{SID}/dev-servers/{p}/open", headers=_H)
               for p in ("abc", "80", "70000", "-1")]
    assert [r.status_code for r in bad] == [422, 422, 422, 422]
    assert fake.dev_calls == []


# ---------------------------------------------------------------- the tailnet name


@pytest.mark.asyncio
async def test_host_tailnet_name_comes_from_the_fleet_registry(isolated_db) -> None:
    from server.db import db_session
    from server.db.models import Host
    from server.sessions.fleet_router import host_tailnet_name
    async with db_session() as db:
        db.add(Host(name="rocky", endpoint="https://rocky.tail0dd570.ts.net:9443"))
        db.add(Host(name="dobby", endpoint="unix:///run/viberpit/daemon.sock"))
        db.add(Host(name="loop", endpoint="https://127.0.0.1:9443"))
    assert await host_tailnet_name("rocky") == "rocky.tail0dd570.ts.net"
    assert await host_tailnet_name("ROCKY") == "rocky.tail0dd570.ts.net"
    assert await host_tailnet_name("dobby") is None
    assert await host_tailnet_name("loop") is None
    assert await host_tailnet_name("nowhere") is None
