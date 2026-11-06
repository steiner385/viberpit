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
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from unittest.mock import patch

import pytest
import pytest_asyncio

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


#: The managed worktree base(s) of the fake hosts: every /w/<x> session is a managed
#: worktree; ~ and plain checkouts are not.
_W = ("/w",)
#: The managed worktree roots of the fake host (each session's own cwd).
_MW = frozenset({"/w/a", "/w/b", "/w/c"})


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
    servers = await _detector(host).detect(SID_A, host.keys, bg_commands=_no_bg, managed_worktrees=_MW)
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
               await _detector(host).detect(SID_A, host.keys, bg_commands=_no_bg, managed_worktrees=_MW)}
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
    b = await _detector(host).detect(SID_B, host.keys, bg_commands=_no_bg, managed_worktrees=_MW)
    c = await _detector(host).detect(SID_C, host.keys, bg_commands=_no_bg, managed_worktrees=_MW)
    assert [s["port"] for s in b] == [5300]
    # Codex: found under the shared app-server by the child's own cwd.
    assert [s["port"] for s in c] == [5400]


@pytest.mark.asyncio
async def test_two_sessions_sharing_a_cwd_attribute_nothing_by_cwd(host) -> None:
    keys = host.keys + _keys(("dddddddd-dddd-4ddd-8ddd-dddddddddddd", "", "/w/a"))
    servers = await _detector(host).detect(SID_A, keys, bg_commands=_no_bg, managed_worktrees=_MW)
    ports = sorted(s["port"] for s in servers)
    # A's own CLI still matches by --resume; the cwd-only matches (the detached server
    # and the Codex child) are ambiguous and dropped.
    assert ports == [5173, 5180, 8080]


_HOME_WT = "/home/u/GitHub/viberpit-sessions/worktrees"


def _helper_cwd_under(home: str, tmp_home: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Where ``helper_call.helper_cwd()`` puts a helper for a user whose home is *home*
    — derived from the helper itself (run against a scratch ``$HOME``), never
    re-spelled here."""
    from server.sessions.helper_call import helper_cwd
    monkeypatch.delenv("VIBERPIT_HELPER_CWD", raising=False)
    monkeypatch.setenv("HOME", str(tmp_home))
    rel = Path(helper_cwd()).relative_to(tmp_home)
    return f"{home}/{rel}"


@pytest.fixture
def home_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """A live session A whose cwd is the home directory (not a managed worktree), two
    Codex sessions in managed worktrees sharing the account's app-server (cwd = the
    Codex home, under ~), and daemon helpers whose cwds sit under ~."""
    p = FakeProc(tmp_path / "proc")
    p.add(100, 1, ["/usr/bin/python3", "-m", "server.ipc"], cwd="/srv/viberpit")
    p.add(200, 100, ["claude"], cwd="/home/u")  # A, no --resume: exact cwd
    p.add(201, 200, ["bash", "-c", "npm run dev"], cwd="/home/u")
    p.add(202, 201, ["node", "vite"], cwd="/home/u/site", sockets=(9001,))
    p.listen(9001, "127.0.0.1", 5173)
    p.add(600, 100, ["/usr/bin/codex", "app-server"], cwd="/home/u/.viberpit/codex/acct")
    # Each Codex thread's server, inside its own managed worktree: that thread's.
    p.add(601, 600, ["bash", "-lc", "npm run dev"], cwd=f"{_HOME_WT}/r/c1")
    p.add(602, 601, ["node", "vite"], cwd=f"{_HOME_WT}/r/c1", sockets=(9020,))
    p.listen(9020, "127.0.0.1", 5400)
    p.add(611, 600, ["bash", "-lc", "pnpm dev"], cwd=f"{_HOME_WT}/r/c2/web")
    p.add(612, 611, ["node", "next"], cwd=f"{_HOME_WT}/r/c2/web", sockets=(9021,))
    p.listen(9021, "127.0.0.1", 5401)
    # A Codex thread's server in ~/scratch: in no managed worktree → nobody's.
    p.add(621, 600, ["bash", "-lc", "python -m http.server"], cwd="/home/u/scratch")
    p.add(622, 621, ["python", "-m", "http.server", "5402"], cwd="/home/u/scratch",
          sockets=(9022,))
    p.listen(9022, "127.0.0.1", 5402)
    # An app-server child inheriting the Codex home: nobody's.
    p.add(631, 600, ["bash", "-lc", "serve"], cwd="/home/u/.viberpit/codex/acct")
    p.add(632, 631, ["node", "serve"], cwd="/home/u/.viberpit/codex/acct", sockets=(9023,))
    p.listen(9023, "127.0.0.1", 5403)
    # A daemon helper root whose worker serves from elsewhere: nobody's.
    p.add(700, 100, ["copilot2api", "--port", "0"], cwd="/home/u/.cache/viberpit")
    p.add(701, 700, ["bash", "-c", "worker"], cwd="/opt/proxy")
    p.add(702, 701, ["worker"], cwd="/opt/proxy", sockets=(9030,))
    p.listen(9030, "127.0.0.1", 5500)
    # A helper child inheriting helper_cwd() (under ~/.cache): nobody's.
    helper = _helper_cwd_under("/home/u", tmp_path / "home", monkeypatch)
    p.add(710, 100, ["python3", "-m", "helper"], cwd=helper)
    p.add(711, 710, ["bash", "-c", "x"], cwd=helper)
    p.add(712, 711, ["node", "x"], cwd=helper, sockets=(9031,))
    p.listen(9031, "127.0.0.1", 5501)
    # Escaped from every subtree, in the daemon's cgroup, cwd under ~: nobody's.
    p.add(800, 1, ["node", "server.js"], cwd="/home/u/site", sockets=(9040,))
    p.listen(9040, "127.0.0.1", 5600)
    # Escaped, inside C1's managed worktree: C1's.
    p.add(801, 1, ["node", "server.js"], cwd=f"{_HOME_WT}/r/c1/api", sockets=(9041,))
    p.listen(9041, "127.0.0.1", 5601)
    keys = _keys((SID_A, "", "/home/u"), (SID_B, "", f"{_HOME_WT}/r/c1"),
                 (SID_C, "", f"{_HOME_WT}/r/c2"))
    probe = _Probe({port: None for port in (5173, 5400, 5401, 5402, 5403, 5500, 5501,
                                            5600, 5601)})
    return SimpleNamespace(fs=ds.ProcFs(str(p.root)), keys=keys, probe=probe)


@pytest.mark.asyncio
async def test_a_home_directory_session_never_claims_other_sessions_servers(home_host) -> None:
    """Outside a session's own subtree, only a cwd inside a MANAGED worktree attributes:
    ~ (not managed) claims nothing there, so nothing there is even probed."""
    det = ds.DevServerDetector(fs=home_host.fs, daemon_pid=100, probe=home_host.probe)
    base = frozenset({f"{_HOME_WT}/r/c1", f"{_HOME_WT}/r/c2"})
    a = await det.detect(SID_A, home_host.keys, bg_commands=_no_bg, managed_worktrees=base)
    assert [s["port"] for s in a] == [5173]
    assert {port for _, port in home_host.probe.calls} == {5173}
    b = await det.detect(SID_B, home_host.keys, bg_commands=_no_bg, managed_worktrees=base)
    c = await det.detect(SID_C, home_host.keys, bg_commands=_no_bg, managed_worktrees=base)
    assert sorted(s["port"] for s in b) == [5400, 5601]
    assert [s["port"] for s in c] == [5401]
    probed = {port for _, port in home_host.probe.calls}
    assert not probed & {5402, 5403, 5500, 5501, 5600}


@pytest.mark.asyncio
async def test_without_managed_worktrees_nothing_outside_a_subtree_attributes(home_host) -> None:
    det = ds.DevServerDetector(fs=home_host.fs, daemon_pid=100, probe=home_host.probe)
    b = await det.detect(SID_B, home_host.keys, bg_commands=_no_bg, managed_worktrees=frozenset())
    assert b == [] and home_host.probe.calls == []


@pytest.mark.asyncio
async def test_a_symlinked_session_cwd_matches_through_realpath(tmp_path: Path) -> None:
    base = tmp_path / "worktrees"
    real = base / "repo" / "s1"
    (real / "api").mkdir(parents=True)
    link = tmp_path / "link-to-s1"
    link.symlink_to(real)
    p = FakeProc(tmp_path / "proc")
    p.add(100, 1, ["python3"], cwd="/srv")
    p.add(200, 100, ["claude"], cwd=str(real))  # the CLI reports the real path
    p.add(201, 200, ["bash", "-c", "dev"], cwd=str(real))
    p.add(202, 201, ["node"], cwd=str(real), sockets=(9001,))
    p.listen(9001, "127.0.0.1", 5173)
    p.add(300, 1, ["node", "escaped.js"], cwd=str(real / "api"), sockets=(9002,))
    p.listen(9002, "127.0.0.1", 5174)
    probe = _Probe({5173: None, 5174: None})
    det = ds.DevServerDetector(fs=ds.ProcFs(str(p.root)), daemon_pid=100, probe=probe)
    keys = [SessionKey(SID_A, "", str(link))]  # the record holds the symlink
    servers = await det.detect(SID_A, keys, bg_commands=_no_bg, managed_worktrees=frozenset({str(real)}))
    assert sorted(s["port"] for s in servers) == [5173, 5174]


@pytest.fixture
def grouping_world(tmp_path: Path):
    """A daemon over a /proc-shaped tree and a real managed-worktree layout:
    ``<repo_root>/<ns>/worktrees/<clone>/<short id>``. Session G sits at the
    ``<clone>`` GROUPING dir; session W at a real worktree root; an archived session's
    ``nohup`` server escaped in a sibling worktree no live session owns."""
    from viberpit import worktrees
    repo_root = tmp_path.resolve() / "GitHub"
    group = worktrees.worktrees_base(str(repo_root)) / "viberpit"
    wt_w, wt_old = group / "aaaaaaaa", group / "deadbeef"
    for d in (wt_w, wt_old):
        d.mkdir(parents=True)
    p = FakeProc(tmp_path / "proc")
    p.add(100, 1, ["python3"], cwd="/srv")
    p.add(300, 1, ["node", "server.js"], cwd=str(wt_old), sockets=(9001,))  # escaped
    p.listen(9001, "127.0.0.1", 5173)
    p.add(301, 1, ["node", "server.js"], cwd=str(wt_w / "api"), sockets=(9002,))
    p.listen(9002, "127.0.0.1", 5174)
    probe = _Probe({5173: None, 5174: None})
    o = SessionOrchestrator(sdk_factory=mock.MagicMock(), poll_interval=9999.0,
                            usage_fn=lambda name: None)
    for sid, cwd in ((SID_A, group), (SID_B, wt_w)):
        o._records[sid] = _rec(sid, str(cwd))
        o._drivers[sid] = mock.MagicMock()
    o._dev_server_det = ds.DevServerDetector(fs=ds.ProcFs(str(p.root)), daemon_pid=100,
                                             probe=probe)

    async def roots():
        return {str(repo_root)}

    async def no_bg(_sid):
        return []
    o._managed_roots = roots  # type: ignore[method-assign]
    o._open_background_work = no_bg  # type: ignore[method-assign]
    return SimpleNamespace(o=o, probe=probe)


def test_session_worktree_root_is_the_clone_and_id_level(tmp_path: Path) -> None:
    from viberpit import worktrees
    root = str(tmp_path.resolve())
    base = worktrees.worktrees_base(root)
    (base / "repo" / "abc" / "sub").mkdir(parents=True)
    assert worktrees.session_worktree_root(str(base / "repo" / "abc"), root) == base / "repo" / "abc"
    assert worktrees.session_worktree_root(str(base / "repo" / "abc" / "sub"), root) == \
        base / "repo" / "abc"
    assert worktrees.session_worktree_root(str(base / "repo"), root) is None  # grouping
    assert worktrees.session_worktree_root(str(base), root) is None
    assert worktrees.session_worktree_root(str(tmp_path), root) is None


@pytest.mark.asyncio
async def test_a_session_at_the_grouping_dir_claims_nothing_outside_its_subtree(
        grouping_world) -> None:
    """Fix round 3, item 1: only a session whose cwd IS a worktree root
    (``<base>/<clone>/<id>``) is managed. A session at ``<base>/<clone>`` is not, so it
    never claims an escaped server in a sibling worktree."""
    o = grouping_world.o
    g = await o._detect_dev_servers(SID_A, fresh=True)
    assert g == []
    assert grouping_world.probe.calls == []
    w = await o._detect_dev_servers(SID_B, fresh=True)
    assert [s["port"] for s in w] == [5174]  # its own worktree still works


@pytest.mark.asyncio
async def test_process_cwds_are_never_resolved(host, monkeypatch) -> None:
    """Fix round 3, item 2: ``realpath`` stats every component, which can block on a
    stalled FUSE mount. ``/proc/<pid>/cwd`` is already canonical, so only SESSION cwds
    are resolved."""
    resolved: list[str] = []
    real = ds._realpath

    def spy(path):
        resolved.append(path)
        return real(path)
    monkeypatch.setattr(ds, "_realpath", spy)
    await _detector(host).detect(SID_A, host.keys, bg_commands=_no_bg, managed_worktrees=_MW)
    assert set(resolved) <= {k.cwd for k in host.keys}


@pytest.mark.asyncio
async def test_a_stuck_scan_times_out_empty_and_logs_info(host, caplog) -> None:
    import logging
    import threading
    caplog.set_level(logging.INFO, logger="server.sessions.dev_servers")
    release = threading.Event()

    class _StuckFs(ds.ProcFs):
        def listeners(self):
            release.wait(5)  # a stat on a stalled mount
            return {}
    det = ds.DevServerDetector(fs=_StuckFs(host.fs.root), daemon_pid=100, probe=host.probe,
                               scan_timeout=0.1)
    try:
        t0 = time.monotonic()
        assert await det.detect(SID_A, host.keys, bg_commands=_no_bg, managed_worktrees=_MW) == []
        assert time.monotonic() - t0 < 2
    finally:
        release.set()
    assert any(r.levelno == logging.INFO and "dev_servers.scan_timeout" in r.getMessage()
               for r in caplog.records)


@pytest.mark.asyncio
async def test_a_scan_stuck_forever_holds_one_thread_and_polls_stay_prompt(
        host, monkeypatch) -> None:
    """Final review item 2: one stalled mount must not leak a thread per poll. While a
    scan is blocked, no second one starts; every poll answers at once with the last
    good result; and the shared default executor is never involved."""
    import threading
    monkeypatch.setattr(ds, "LAST_GOOD_MAX_AGE_S", 1e9)  # age-out has its own test
    block, entered = threading.Event(), []
    now = [1000.0]

    class _StuckFs(ds.ProcFs):
        def listeners(self):
            entered.append(threading.current_thread())
            if len(entered) > 1:
                block.wait()  # every scan after the first: a stalled mount, forever
            return super().listeners()
    det = ds.DevServerDetector(fs=_StuckFs(host.fs.root), daemon_pid=100, probe=host.probe,
                               scan_timeout=0.2, clock=lambda: now[0])
    try:
        good = await det.detect(SID_A, host.keys, bg_commands=_no_bg, managed_worktrees=_MW)
        assert [s["port"] for s in good] == [3000, 5173, 5180, 5401, 8080]
        before = threading.active_count()
        t0 = time.monotonic()
        polls = []
        for _ in range(20):
            now[0] += ds.CACHE_TTL_S + 1  # never served from the cache
            polls.append(await det.detect(SID_A, host.keys, bg_commands=_no_bg,
                                          managed_worktrees=_MW))
        elapsed = time.monotonic() - t0
        assert len(entered) == 2  # the good scan, then ONE stuck scan — not 20
        assert threading.active_count() - before <= 1
        assert elapsed < 0.2 + 1.0  # one timeout, then every poll returns at once
        assert all(p == good for p in polls)  # the last good answer meanwhile
        assert entered[1] is not threading.main_thread()
        # The loop's default executor is untouched and still serves other work.
        assert await asyncio.wait_for(asyncio.to_thread(lambda: 42), 2) == 42
        # A fresh (Open) detection never trusts a stale answer.
        assert await det.detect(SID_A, host.keys, bg_commands=_no_bg, fresh=True,
                                managed_worktrees=_MW) == []
    finally:
        block.set()


@pytest.mark.asyncio
async def test_last_good_list_ages_out_while_a_scan_stays_stuck(host) -> None:
    """Dead servers must not stay listed forever behind a stuck scan."""
    import threading
    block, entered = threading.Event(), []
    now = [1000.0]

    class _StuckFs(ds.ProcFs):
        def listeners(self):
            entered.append(1)
            if len(entered) > 1:
                block.wait()
            return super().listeners()
    det = ds.DevServerDetector(fs=_StuckFs(host.fs.root), daemon_pid=100, probe=host.probe,
                               scan_timeout=0.1, clock=lambda: now[0])
    try:
        good = await det.detect(SID_A, host.keys, bg_commands=_no_bg, managed_worktrees=_MW)
        assert good
        now[0] += ds.CACHE_TTL_S + 1
        assert await det.detect(SID_A, host.keys, bg_commands=_no_bg,
                                managed_worktrees=_MW) == good
        now[0] += ds.LAST_GOOD_MAX_AGE_S + 1
        assert await det.detect(SID_A, host.keys, bg_commands=_no_bg,
                                managed_worktrees=_MW) == []
    finally:
        block.set()


@pytest.mark.asyncio
async def test_stuck_managed_root_resolution_is_inside_the_one_bounded_scan(
        grouping_world, monkeypatch) -> None:
    """Final review item 2: resolving the sessions' managed worktree roots stats their
    cwds too, so it runs inside the same single bounded scan — never a separate
    unbounded ``to_thread`` per poll."""
    import threading
    from viberpit import worktrees
    block, calls = threading.Event(), []

    def stuck(path, repo_root):
        calls.append(path)
        block.wait()
        return None
    monkeypatch.setattr(worktrees, "session_worktree_root", stuck)
    o = grouping_world.o
    o._dev_server_det._scan_timeout = 0.2
    try:
        t0 = time.monotonic()
        for _ in range(20):
            assert await asyncio.wait_for(o._detect_dev_servers(SID_B, fresh=True), 3) == []
        assert time.monotonic() - t0 < 0.2 + 1.0
        assert len(calls) == 1  # one thread, blocked on the first cwd
        assert await asyncio.wait_for(asyncio.to_thread(lambda: 42), 2) == 42
    finally:
        block.set()


@pytest.mark.asyncio
async def test_unknown_session_lists_nothing_and_probes_nothing(host) -> None:
    servers = await _detector(host).detect("nope", host.keys, bg_commands=_no_bg, managed_worktrees=_MW)
    assert servers == [] and host.probe.calls == []


# ---------------------------------------------------------------- labels, cache


@pytest.mark.asyncio
async def test_label_prefers_the_background_task_command(host) -> None:
    async def bg() -> list[str]:
        return ["cd site && python -m http.server 5173", "unrelated"]
    servers = {s["port"]: s for s in await _detector(host).detect(SID_A, host.keys, bg_commands=bg, managed_worktrees=_MW)}
    assert servers[5173]["label"] == "cd site && python -m http.server 5173"
    assert servers[8080]["label"] == "node vite --host"


@pytest.mark.asyncio
async def test_results_are_cached_briefly_and_fresh_rescans(host) -> None:
    now = [1000.0]
    det = _detector(host, clock=lambda: now[0])
    await det.detect(SID_A, host.keys, bg_commands=_no_bg, managed_worktrees=_MW)
    n = len(host.probe.calls)
    await det.detect(SID_A, host.keys, bg_commands=_no_bg, managed_worktrees=_MW)
    assert len(host.probe.calls) == n  # cached
    await det.detect(SID_A, host.keys, bg_commands=_no_bg, fresh=True,
                                     managed_worktrees=_MW)
    assert len(host.probe.calls) == 2 * n
    now[0] += ds.CACHE_TTL_S + 0.1
    await det.detect(SID_A, host.keys, bg_commands=_no_bg, managed_worktrees=_MW)
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
@pytest.mark.skipif(not os.path.exists("/proc/net/tcp") or shutil.which("bash") is None,
                    reason="needs Linux /proc/net/tcp and bash")
@pytest.mark.asyncio
async def test_real_proc_detects_http_server_and_not_a_raw_socket(tmp_path: Path) -> None:
    """No-model contract test: a stand-in session root (a direct child of this process,
    cwd in its worktree) starts ``python -m http.server`` on 127.0.0.1 and a raw
    listener. The real ``/proc`` scan finds the server and never lists the raw socket.
    Both bind port 0 and report the port they got — no pick-then-rebind race."""
    work = tmp_path / "worktree"
    work.mkdir()
    (work / "index.html").write_text("<title>stand-in</title>")
    raw = ("import socket,sys,time\ns=socket.socket();s.bind(('127.0.0.1',0));s.listen()\n"
           "print('RAW', s.getsockname()[1],flush=True)\ntime.sleep(60)\n")
    (work / "raw.py").write_text(raw)
    script = (f"{sys.executable} -u -m http.server 0 --bind 127.0.0.1 2>/dev/null & "
              f"{sys.executable} raw.py & wait")
    root = subprocess.Popen(["bash", "-c", script], cwd=work, stdout=subprocess.PIPE,
                            text=True, start_new_session=True)
    try:
        assert root.stdout is not None
        port = raw_port = 0
        while not (port and raw_port):
            line = await asyncio.wait_for(asyncio.to_thread(root.stdout.readline), 10)
            if not line:  # EOF: both children died before reporting their ports
                pytest.fail(f"stand-in servers exited early (rc={root.poll()})")
            if line.startswith("RAW "):
                raw_port = int(line.split()[1])
            elif line.startswith("Serving HTTP on"):  # "… port N (http://…/) …"
                port = int(line.split(" port ")[1].split()[0])
        keys = [SessionKey(SID_A, "", str(work))]
        det = ds.DevServerDetector(daemon_pid=os.getpid())
        servers: list[dict] = []
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            servers = await det.detect(SID_A, keys, bg_commands=_no_bg, fresh=True,
                                     managed_worktrees=_MW)
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
                                 bg_commands=_no_bg, fresh=True,
                                     managed_worktrees=_MW)
        assert other == []
    finally:
        os.killpg(root.pid, 15)
        root.wait(timeout=10)


# ---------------------------------------------------------------- routing (RF 3)


_LOOP = {"port": 5173, "address": "127.0.0.1", "bind": "loopback", "pid": 1, "label": "x"}
_V6 = {"port": 5180, "address": "::1", "bind": "loopback", "pid": 1, "label": "x"}
_ALL = {"port": 8080, "address": "127.0.0.1", "bind": "all", "pid": 1, "label": "x",
        "note": ds.EXPOSED_NOTE}
_LOOP2 = {"port": 5174, "address": "127.0.0.2", "bind": "loopback", "pid": 1, "label": "x"}
_ALL6 = {"port": 8081, "address": "::1", "bind": "all", "pid": 1, "label": "x",
         "note": ds.EXPOSED_NOTE}
_IP = "100.101.102.103"
_IP6 = "fd7a:115c:a1e0::1"


@pytest.mark.parametrize("server,client_node,tailnet,expected", [
    # local-link: the detected address itself (127.0.0.2 is not "localhost").
    (_LOOP, "rocky", _IP, {"kind": "local-link", "url": "http://127.0.0.1:5173/"}),
    (_LOOP, "ROCKY", None, {"kind": "local-link", "url": "http://127.0.0.1:5173/"}),
    (_LOOP2, "rocky", None, {"kind": "local-link", "url": "http://127.0.0.2:5174/"}),
    (_V6, "rocky", None, {"kind": "local-link", "url": "http://[::1]:5180/"}),
    (_ALL, "rocky", _IP, {"kind": "local-link", "url": "http://127.0.0.1:8080/"}),
    (_LOOP, "phone", _IP, {"kind": "vnc"}),
    (_LOOP, None, _IP, {"kind": "vnc"}),
    # tailnet-link: the host's tailnet IP (Vite accepts IP literals as Host).
    (_ALL, "phone", _IP,
     {"kind": "tailnet-link", "url": f"http://{_IP}:8080/", "note": ds.EXPOSED_NOTE}),
    (_ALL6, None, _IP6,
     {"kind": "tailnet-link", "url": f"http://[{_IP6}]:8081/", "note": ds.EXPOSED_NOTE}),
    # No known tailnet IP, or not an IP at all (never a name): vnc.
    (_ALL, "phone", None, {"kind": "vnc"}),
    (_ALL, "phone", "rocky.tail0dd570.ts.net", {"kind": "vnc"}),
])
def test_open_route(server, client_node, tailnet, expected) -> None:
    assert ds.open_route(server, client_node=client_node, runner="rocky",
                         tailnet_ip=tailnet) == expected


@pytest.mark.parametrize("server,client_node,tailnet,expected", [
    # Final review item 7: a host without X11 has no vnc route to offer.
    (_LOOP, "phone", _IP, {"kind": "none", "reason": ds.NO_ROUTE_NO_DISPLAY}),
    (_ALL, "phone", None, {"kind": "none", "reason": ds.NO_ROUTE_NO_DISPLAY}),
    # The links do not need the host's display.
    (_LOOP, "rocky", None, {"kind": "local-link", "url": "http://127.0.0.1:5173/"}),
    (_ALL, "phone", _IP,
     {"kind": "tailnet-link", "url": f"http://{_IP}:8080/", "note": ds.EXPOSED_NOTE}),
])
def test_open_route_without_x11_on_the_host(server, client_node, tailnet, expected) -> None:
    assert ds.open_route(server, client_node=client_node, runner="rocky",
                         tailnet_ip=tailnet, x11=False) == expected


@pytest.mark.parametrize("own,expected", [
    (["fd7a:115c:a1e0::1", "100.101.102.103"], "100.101.102.103"),  # IPv4 preferred
    (["fd7a:115c:a1e0::1"], "fd7a:115c:a1e0::1"),
    ([], None),
])
def test_self_tailnet_ip_reads_the_hosts_own_tailscale_status(own, expected) -> None:
    with patch("server.sessions.dev_servers._self_tailscale_ips",
               return_value=frozenset(ipaddress.ip_address(a) for a in own)):
        assert ds.self_tailnet_ip() == expected


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
        self._specs.setdefault(sid, SimpleNamespace(browsers=[]))
        return self._specs[sid]

    async def stop_display(self, sid):
        self.stopped.append(sid)
        self._specs.pop(sid, None)

    async def launch_browser(self, sid, url):
        self.launched.append((sid, url))
        return self.launch_ok

    def prune_browsers(self, sid):
        pass

    async def stop_preview(self, sid):
        self.previews_stopped = getattr(self, "previews_stopped", 0) + 1

    def track_preview(self, sid, *, release_display):
        self.tracked = getattr(self, "tracked", []) + [(sid, release_display)]


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
    bases: list[set[str]] = []

    async def detect(session_id, sessions, *, bg_commands, fresh=False, repo_roots=()):
        calls.append((session_id, [k.session_id for k in sessions], fresh))
        bases.append(set(repo_roots))
        return [dict(s) for s in found]
    o._dev_server_detector().detect = detect  # type: ignore[method-assign]
    return SimpleNamespace(o=o, found=found, calls=calls, bases=bases)


@pytest.mark.asyncio
async def test_orchestrator_dev_servers_names_this_host(orch) -> None:
    with patch("server.sessions.orchestrator.self_host_name", return_value="rocky"), \
            patch("server.sessions.dev_servers.self_tailnet_ip") as ip:
        res = await orch.o.dev_servers(SID_A)
    # No bind-all server: no tailnet IP is looked up.
    assert res == {"host": "rocky", "enabled": True, "x11": True, "tailnetIp": None,
                   "servers": orch.found}
    ip.assert_not_called()
    # The managed worktrees are resolved from the same roots as _ensure_session_cwd,
    # inside the detector's bounded scan.
    assert orch.bases[-1] == await orch.o._managed_roots()
    assert orch.calls == [(SID_A, [SID_A], False)]
    with pytest.raises(KeyError):
        await orch.o.dev_servers("nope")


@pytest.mark.asyncio
async def test_orchestrator_reports_its_own_tailnet_ip_for_an_exposed_server(orch) -> None:
    """Item 6: the daemon answers with ITS host's tailnet IP — so a single-host dobby
    whose daemon is a unix socket (no fleet endpoint naming it) still offers a
    tailnet-link."""
    orch.found[:] = [dict(_ALL)]
    with patch("server.sessions.dev_servers._self_tailscale_ips",
               return_value=frozenset({ipaddress.ip_address(_IP)})):
        res = await orch.o.dev_servers(SID_A)
    assert res["tailnetIp"] == _IP


@pytest.mark.asyncio
async def test_uds_daemon_reports_its_tailnet_ip(orch) -> None:
    orch.found[:] = [dict(_ALL)]
    app = create_daemon_app()
    register_orchestrator_rpc(app, orch.o)
    with patch("server.sessions.dev_servers._self_tailscale_ips",
               return_value=frozenset({ipaddress.ip_address(_IP)})):
        async with serve_daemon_uds(app) as sock:
            client = DaemonClient(sock)
            try:
                res = await RemoteOrchestrator(client).dev_servers(SID_A)
            finally:
                await client.aclose()
    assert res["tailnetIp"] == _IP


@pytest.mark.asyncio
async def test_dev_servers_kill_switch_says_it_is_off(orch, monkeypatch) -> None:
    """Final review item 3: off is not "nothing detected" — the answer says so, and an
    Open is refused with its own code, detecting nothing."""
    monkeypatch.setenv("VIBERPIT_DEV_SERVER_DETECT_ENABLED", "false")
    res = await orch.o.dev_servers(SID_A)
    assert res["enabled"] is False
    assert res["servers"] == [] and orch.calls == []
    with pytest.raises(ds.DevServerError) as ei:
        await orch.o.open_dev_server(SID_A, port=5173)
    assert ei.value.code == ds.DEV_SERVER_DETECT_DISABLED
    assert orch.calls == [] and orch.o._x11.started == []
    monkeypatch.setenv("VIBERPIT_DEV_SERVER_DETECT_ENABLED", "true")
    assert (await orch.o.dev_servers(SID_A))["enabled"] is True


@pytest.mark.asyncio
async def test_dev_servers_reports_whether_this_host_has_x11(orch) -> None:
    """Final review item 7: the owning daemon says whether it can show a ``vnc`` preview
    at all, so the edge never offers a route that always refuses."""
    assert (await orch.o.dev_servers(SID_A))["x11"] is True
    orch.o._x11 = None
    assert (await orch.o.dev_servers(SID_A))["x11"] is False


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


# ---------------------------------------------------------------- preview Chromium lifecycle


class _Browser:
    """An asyncio-subprocess stand-in whose ``wait`` blocks until it exits."""

    _next_pid = 10_000_000  # above any pid_max: never a real process

    def __init__(self) -> None:
        _Browser._next_pid += 1
        self.pid = _Browser._next_pid
        self.returncode: int | None = None
        self._exited = asyncio.Event()
        self._loop = asyncio.get_running_loop()

    def exit(self, code: int = 0) -> None:
        # May be called from the to_thread a process-tree kill runs in.
        self.returncode = code
        self._loop.call_soon_threadsafe(self._exited.set)

    def terminate(self) -> None:
        self.exit(-15)

    def kill(self) -> None:
        self.exit(-9)

    async def wait(self) -> int:
        await self._exited.wait()
        assert self.returncode is not None
        return self.returncode


@pytest.fixture
def real_x11(orch, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The orchestrator over a real X11Manager (fake spawn): 6 browsers per session.
    A process-tree kill ends the fake browser with that pid; nothing is signalled."""
    from server.sessions import x11manager
    from server.sessions.x11manager import DisplayAllocator, X11Manager
    browsers: list[_Browser] = []
    argvs: list[tuple[str, ...]] = []

    def kill_tree(pid, **_kw):
        for b in browsers:
            if b.pid == pid and b.returncode is None:
                b.exit(-15)
        return True
    monkeypatch.setattr(x11manager, "terminate_process_tree", kill_tree)
    monkeypatch.setattr(x11manager, "read_start_time", lambda pid: 1)  # fake pids

    async def spawn(*argv, **kw):
        proc = _Browser()
        if argv[0] == "xauth":
            proc.exit()
        if argv[0] == "chromium":
            browsers.append(proc)
            argvs.append(argv)
            await asyncio.sleep(0)  # a real spawn yields: lets concurrent Opens interleave
        return proc
    mgr = X11Manager(DisplayAllocator(100, 2, 6100), resolution="800x600x24", max_browsers=6,
                     spawn=spawn, ready_check=lambda d, xa: True,
                     vnc_ready_check=lambda p: True, run_dir=str(tmp_path),
                     vnc_idle_timeout_s=0.05)
    orch.o._x11 = mgr
    return SimpleNamespace(o=orch.o, mgr=mgr, browsers=browsers, argvs=argvs)


def _live(browsers: list[_Browser]) -> list[_Browser]:
    return [b for b in browsers if b.returncode is None]


@pytest.mark.asyncio
async def test_ten_opens_keep_one_live_preview_chromium(real_x11) -> None:
    real_x11.o._records[SID_A].display_mode = DISPLAY_VIRTUAL
    await real_x11.mgr.start_display(SID_A)
    for _ in range(10):
        res = await real_x11.o.open_dev_server(SID_A, port=5173)
        assert res["ok"] is True
    assert len(real_x11.browsers) == 10
    assert len(_live(real_x11.browsers)) == 1
    spec = real_x11.mgr.display_for(SID_A)
    assert spec is not None and spec.browsers == _live(real_x11.browsers)
    # A virtual session's display is the session's own: it stays.
    real_x11.browsers[-1].exit()
    await asyncio.sleep(0.2)
    assert real_x11.mgr.display_for(SID_A) is not None


@pytest.mark.asyncio
async def test_open_goes_through_the_hardened_launcher(real_x11) -> None:
    """After the security PR (#3324): the preview launch is X11Manager.launch_browser,
    so the URL is is_browser_url-checked and passed after ``--`` (never a switch)."""
    real_x11.o._records[SID_A].display_mode = DISPLAY_VIRTUAL
    await real_x11.mgr.start_display(SID_A)
    assert (await real_x11.o.open_dev_server(SID_A, port=5173))["ok"] is True
    assert real_x11.argvs[-1][-2:] == ("--", "http://127.0.0.1:5173/")


@pytest.mark.asyncio
async def test_the_cap_counts_live_browsers_only(real_x11) -> None:
    """Six windows the user opened and closed again never block a preview."""
    real_x11.o._records[SID_A].display_mode = DISPLAY_VIRTUAL
    await real_x11.mgr.start_display(SID_A)
    for _ in range(6):  # "Open a browser" up to the cap, each window closed after
        assert (await real_x11.o.open_browser(SID_A, None))["ok"] is True
        real_x11.browsers[-1].exit()
    assert (await real_x11.o.open_dev_server(SID_A, port=5173))["ok"] is True
    assert len(_live(real_x11.browsers)) == 1


@pytest.mark.asyncio
async def test_a_native_sessions_preview_display_is_released_when_its_browser_exits(
        real_x11) -> None:
    for _ in range(3):
        assert (await real_x11.o.open_dev_server(SID_A, port=5173))["ok"] is True
    assert len(_live(real_x11.browsers)) == 1
    assert real_x11.mgr.display_for(SID_A) is not None
    real_x11.browsers[-1].exit()  # the user closed the preview window
    for _ in range(50):
        if real_x11.mgr.display_for(SID_A) is None:
            break
        await asyncio.sleep(0.01)
    assert real_x11.mgr.display_for(SID_A) is None
    # Xvfb gone and the pool lease back: both displays allocatable again.
    assert real_x11.mgr.metrics()["displays_allocated"] == 0
    assert real_x11.o._records[SID_A].display_mode == DISPLAY_NATIVE


@pytest.mark.asyncio
async def test_a_native_sessions_idle_preview_display_is_released(real_x11) -> None:
    """Nobody attached a viewer within the VNC idle bound: the preview display goes,
    the browser with it."""
    assert (await real_x11.o.open_dev_server(SID_A, port=5173))["ok"] is True
    for _ in range(100):
        if real_x11.mgr.display_for(SID_A) is None:
            break
        await asyncio.sleep(0.01)
    assert real_x11.mgr.display_for(SID_A) is None
    for _ in range(100):  # stop_display forgets the spec first, then kills
        if not _live(real_x11.browsers):
            break
        await asyncio.sleep(0.01)
    assert _live(real_x11.browsers) == []


@pytest.mark.asyncio
async def test_a_watched_native_preview_display_stays_while_viewed(real_x11) -> None:
    assert (await real_x11.o.open_dev_server(SID_A, port=5173))["ok"] is True
    await real_x11.mgr.attach(SID_A)
    await asyncio.sleep(0.2)  # several idle bounds
    assert real_x11.mgr.display_for(SID_A) is not None
    real_x11.mgr.detach(SID_A)
    for _ in range(100):
        if real_x11.mgr.display_for(SID_A) is None:
            break
        await asyncio.sleep(0.01)
    assert real_x11.mgr.display_for(SID_A) is None


@pytest.mark.asyncio
async def test_a_native_preview_display_is_released_only_after_a_whole_unwatched_timeout(
        real_x11) -> None:
    """Final review item 6: "idle" is unwatched for the WHOLE timeout, measured from the
    last viewer's detach — not "no viewer at the moment the reaper ticks"."""
    mgr = real_x11.mgr
    mgr._vnc_idle_timeout_s = 0.4
    t0 = time.monotonic()
    assert (await real_x11.o.open_dev_server(SID_A, port=5173))["ok"] is True
    await mgr.attach(SID_A)
    await asyncio.sleep(0.3 - (time.monotonic() - t0))
    mgr.detach(SID_A)  # watched until t0+0.3
    await asyncio.sleep(0.55 - (time.monotonic() - t0))
    # The reaper ticked at ~t0+0.4 with nobody attached, but nobody has been unwatched
    # for 0.4 s yet: the display stays.
    assert mgr.display_for(SID_A) is not None
    for _ in range(150):
        if mgr.display_for(SID_A) is None:
            break
        await asyncio.sleep(0.01)
    assert mgr.display_for(SID_A) is None
    assert time.monotonic() - t0 >= 0.3 + 0.4


@pytest.mark.asyncio
async def test_stop_display_never_signals_a_reused_or_exited_browser_pid(sleeper_x11) -> None:
    """Final review item 8: display teardown kills its browsers through the start-time
    checked ``terminate_process_tree``, like ``stop_preview`` — never by pid alone."""
    mgr = sleeper_x11.mgr
    assert await mgr.launch_browser(SID_A, None)
    assert await mgr.launch_browser(SID_A, None)
    reused, genuine = sleeper_x11.procs
    spec = mgr.display_for(SID_A)
    assert spec.browser_starts[reused.pid] is not None
    spec.browser_starts[reused.pid] += 1  # that pid now names another process
    await mgr.stop_display(SID_A)
    assert await asyncio.wait_for(genuine.wait(), 5) is not None  # positive control
    await asyncio.sleep(0.2)
    assert reused.returncode is None  # not signalled


@pytest.mark.asyncio
async def test_stop_display_never_signals_an_exited_browser(sleeper_x11, monkeypatch) -> None:
    from server.sessions import x11manager
    signalled: list[int] = []

    def spy(pid, **kw):
        signalled.append(pid)
        return True
    monkeypatch.setattr(x11manager, "terminate_process_tree", spy)
    mgr = sleeper_x11.mgr
    assert await mgr.launch_browser(SID_A, None)
    proc = sleeper_x11.procs[0]
    proc.kill()
    await proc.wait()  # exited and reaped: its pid is free for reuse
    await mgr.stop_display(SID_A)
    assert signalled == []


def test_terminate_process_tree_refuses_a_reused_pid() -> None:
    """A recorded start time that no longer matches the pid (it exited and the pid was
    reused) is never signalled; a matching one is."""
    from server.sessions.adopt import terminate_process_tree
    sent: list[tuple[int, int]] = []
    assert terminate_process_tree(4242, killer=lambda pid, sig: sent.append((pid, sig)),
                                  alive=lambda pid: True, grace=0.0, start_time=100,
                                  read_start=lambda pid: 200) is False
    assert sent == []
    assert terminate_process_tree(4242, killer=lambda pid, sig: sent.append((pid, sig)),
                                  alive=lambda pid: not sent, grace=0.0, start_time=100,
                                  read_start=lambda pid: 100) is True
    assert len(sent) == 1


def test_read_start_time_reads_stat_field_22() -> None:
    from server.sessions.adopt import read_start_time
    with open(f"/proc/{os.getpid()}/stat") as fh:
        expected = int(fh.read().rpartition(")")[2].split()[19])
    assert read_start_time(os.getpid()) == expected
    assert read_start_time(2**31 - 1) is None


@pytest_asyncio.fixture
async def sleeper_x11(tmp_path: Path):
    """An X11Manager whose "chromium" is a real ``sleep`` child, killed for real."""
    from server.sessions.x11manager import DisplayAllocator, X11Manager
    procs: list = []

    async def spawn(*argv, **kw):
        if argv[0] == "chromium":
            proc = await asyncio.create_subprocess_exec("sleep", "30")
            procs.append(proc)
            return proc
        p = _Browser()
        if argv[0] == "xauth":
            p.exit()
        return p
    mgr = X11Manager(DisplayAllocator(100, 1, 6100), resolution="800x600x24", max_browsers=6,
                     spawn=spawn, ready_check=lambda d, xa: True,
                     vnc_ready_check=lambda p: True, run_dir=str(tmp_path))
    await mgr.start_display(SID_A)
    try:
        yield SimpleNamespace(mgr=mgr, procs=procs)
    finally:
        for proc in procs:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()


@pytest.mark.asyncio
async def test_stop_preview_never_signals_a_reused_pid(sleeper_x11) -> None:
    """Security: the stored preview's pid now names a different process (simulated by a
    start-time mismatch) — it is left alone."""
    mgr = sleeper_x11.mgr
    assert await mgr.launch_browser(SID_A, None)
    mgr.track_preview(SID_A, release_display=False)
    spec = mgr.display_for(SID_A)
    spec.preview_start = (spec.preview_start or 0) + 1  # the pid was reused
    await mgr.stop_preview(SID_A)
    await asyncio.sleep(0.2)
    assert sleeper_x11.procs[0].returncode is None  # not signalled
    # Positive control: the genuine preview (start time matches) is killed.
    assert await mgr.launch_browser(SID_A, None)
    mgr.track_preview(SID_A, release_display=False)
    await mgr.stop_preview(SID_A)
    assert await asyncio.wait_for(sleeper_x11.procs[1].wait(), 5) is not None


@pytest.mark.asyncio
async def test_stop_preview_never_signals_an_exited_preview(sleeper_x11) -> None:
    mgr = sleeper_x11.mgr
    assert await mgr.launch_browser(SID_A, None)
    mgr.track_preview(SID_A, release_display=False)
    proc = sleeper_x11.procs[0]
    proc.kill()
    await proc.wait()  # exited and reaped: its pid is free for reuse
    with patch("server.sessions.x11manager.terminate_process_tree") as kill:
        await mgr.stop_preview(SID_A)
    kill.assert_not_called()


@pytest.mark.asyncio
async def test_a_preview_without_a_start_time_is_still_reaped(real_x11, monkeypatch) -> None:
    """Fix round 3, item 3: no start time at track time (it could not be read) → the
    preview cannot be safely signalled by pid, so it stays in ``spec.browsers`` for
    ``stop_display`` to reap rather than being forgotten alive."""
    from server.sessions import x11manager
    monkeypatch.setattr(x11manager, "read_start_time", lambda pid: None)
    real_x11.o._records[SID_A].display_mode = DISPLAY_VIRTUAL
    await real_x11.mgr.start_display(SID_A)
    assert (await real_x11.o.open_dev_server(SID_A, port=5173))["ok"] is True
    first = real_x11.browsers[-1]
    assert (await real_x11.o.open_dev_server(SID_A, port=5173))["ok"] is True
    spec = real_x11.mgr.display_for(SID_A)
    assert first in spec.browsers  # still tracked, though no longer the preview
    await real_x11.mgr.stop_display(SID_A)
    assert first.returncode is not None and _live(real_x11.browsers) == []


@pytest.mark.asyncio
async def test_concurrent_opens_leave_exactly_one_tracked_preview(real_x11) -> None:
    real_x11.o._records[SID_A].display_mode = DISPLAY_VIRTUAL
    await real_x11.mgr.start_display(SID_A)
    results = await asyncio.gather(*(real_x11.o.open_dev_server(SID_A, port=5173)
                                     for _ in range(3)))
    assert all(r["ok"] for r in results)
    live = _live(real_x11.browsers)
    assert len(live) == 1
    assert real_x11.mgr.display_for(SID_A).preview is live[0]  # none untracked


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
    with pytest.raises(ds.DevServerError) as off:
        await RemoteOrchestrator(_RecordingClient(error=DaemonError(
            "DevServerError", ds.DEV_SERVER_DETECT_DISABLED,
            {"code": ds.DEV_SERVER_DETECT_DISABLED}))).open_dev_server(SID_A, port=1)
    assert off.value.code == ds.DEV_SERVER_DETECT_DISABLED
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
                  open_error: Exception | None = None, extra: dict | None = None):
    client, fake = _make_client()
    fake.dev_calls = []

    async def dev_servers(sid):
        fake.dev_calls.append(("dev_servers", sid))
        if error is not None:
            raise error
        return {"host": host_name, "tailnetIp": fake.tailnet_ip,
                "servers": [dict(s) for s in _DETECTED], **(extra or {})}

    async def open_dev_server(sid, *, port):
        fake.dev_calls.append(("open_dev_server", sid, port))
        if open_error is not None:
            raise open_error
        return {"ok": True, "port": port, "url": f"http://127.0.0.1:{port}/"}
    fake.tailnet_ip = _IP
    fake.dev_servers = dev_servers
    fake.open_dev_server = open_dev_server
    return client, fake


_NODE = "server.api.sessions.client_node_name"
@pytest.mark.parametrize("viewer,tailnet,kinds", [
    ("rocky", _IP, ["local-link", "local-link"]),
    ("dobby", _IP, ["vnc", "tailnet-link"]),
    ("phone", None, ["vnc", "vnc"]),
    (None, _IP, ["vnc", "tailnet-link"]),
])
def test_get_routes_open_per_viewer(viewer, tailnet, kinds) -> None:
    client, fake = _route_client()
    fake.tailnet_ip = tailnet
    with patch.dict(os.environ, _ENV), patch(_NODE, return_value=viewer), client:
        r = client.get(f"/api/sessions/{SID}/dev-servers", headers=_H)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["host"] == "rocky"
    assert [s["open"]["kind"] for s in body["servers"]] == kinds
    if "tailnet-link" in kinds:
        assert body["servers"][1]["open"]["url"] == f"http://{_IP}:8080/"
    assert fake.dev_calls == [("dev_servers", SID)]


def test_get_says_detection_is_off() -> None:
    """Final review item 3: the web hides the section on ``enabled: false``."""
    client, fake = _route_client(extra={"enabled": False, "servers": []})
    with patch.dict(os.environ, _ENV), patch(_NODE, return_value="phone"), client:
        r = client.get(f"/api/sessions/{SID}/dev-servers", headers=_H)
    assert r.status_code == 200, r.text
    assert r.json() == {"host": "rocky", "enabled": False, "servers": []}


def test_get_offers_no_vnc_route_from_a_host_without_x11() -> None:
    """Final review item 7: ``vnc`` on a host with no X11 always 409s — offer
    ``{kind: "none", reason}`` instead; a link that needs no display stays."""
    client, _ = _route_client(extra={"x11": False})
    with patch.dict(os.environ, _ENV), patch(_NODE, return_value="phone"), client:
        r = client.get(f"/api/sessions/{SID}/dev-servers", headers=_H)
    assert r.status_code == 200, r.text
    assert [s["open"] for s in r.json()["servers"]] == [
        {"kind": "none", "reason": ds.NO_ROUTE_NO_DISPLAY},
        {"kind": "tailnet-link", "url": f"http://{_IP}:8080/", "note": ds.EXPOSED_NOTE}]


def test_get_from_the_session_host_links_to_the_detected_address() -> None:
    client, _ = _route_client()
    with patch.dict(os.environ, _ENV), patch(_NODE, return_value="rocky"), client:
        body = client.get(f"/api/sessions/{SID}/dev-servers", headers=_H).json()
    assert body["servers"][0]["open"] == {"kind": "local-link", "url": "http://127.0.0.1:5173/"}
    assert "tailnetIp" not in body  # the daemon's IP is used for routes, never echoed


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
    client, fake = _route_client()
    fake.tailnet_ip = None
    # The forwarding hop is a trusted proxy, so the request authenticates (200) and the
    # locality assertion below always runs.
    env = {**_ENV, "VIBERPIT_TRUSTED_PROXY_IPS": claimed}
    with patch.dict(os.environ, env), client:
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
    assert forwarded.status_code == 200, forwarded.text
    assert [s["open"]["kind"] for s in forwarded.json()["servers"]] == ["vnc", "vnc"]


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
    (ds.DevServerError(ds.DEV_SERVER_DETECT_DISABLED), 404, ds.DEV_SERVER_DETECT_DISABLED),
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


def test_post_open_cross_origin_is_refused_by_the_origin_guard() -> None:
    """The body-less mutating POST is covered by the Origin allowlist (#3324): a
    cross-origin page cannot make the session's host open a browser."""
    from server.auth import origin as og
    client, fake = _route_client()
    with patch.dict(os.environ, _ENV), client:
        evil = client.post(f"/api/sessions/{SID}/dev-servers/5173/open",
                           headers={**_H, "Origin": "https://evil.example"})
        cross = client.post(f"/api/sessions/{SID}/dev-servers/5173/open",
                            headers={**_H, "Sec-Fetch-Site": "cross-site"})
        native = client.post(f"/api/sessions/{SID}/dev-servers/5173/open", headers=_H)
    assert evil.status_code == 403 and evil.json()["error"]["code"] == og.REFUSAL_CODE
    assert cross.status_code == 403
    assert native.status_code == 200  # no Origin (native client) reaches the route
    assert fake.dev_calls == [("open_dev_server", SID, 5173)]  # only the native one


def test_post_open_rejects_a_non_port_path() -> None:
    client, fake = _route_client()
    with patch.dict(os.environ, _ENV), client:
        bad = [client.post(f"/api/sessions/{SID}/dev-servers/{p}/open", headers=_H)
               for p in ("abc", "80", "70000", "-1")]
    assert [r.status_code for r in bad] == [422, 422, 422, 422]
    assert fake.dev_calls == []
