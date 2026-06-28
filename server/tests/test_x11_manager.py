# server/tests/test_x11_manager.py
import pytest
from server.sessions.x11manager import DisplayAllocator, X11Manager

class FakeProc:
    def __init__(self): self.pid = 4242; self._terminated = False; self.returncode = None
    def terminate(self): self._terminated = True; self.returncode = 0
    def kill(self): self.returncode = -9
    async def wait(self): return self.returncode if self.returncode is not None else 0

@pytest.mark.asyncio
async def test_start_display_injects_env_when_ready(tmp_path):
    spawned = []
    async def fake_spawn(*argv, **kw):
        spawned.append(argv); return FakeProc()
    mgr = X11Manager(DisplayAllocator(100, 4, 6100), resolution="800x600x24",
                     max_browsers=2, spawn=fake_spawn,
                     ready_check=lambda d: True, run_dir=str(tmp_path))
    spec = await mgr.start_display("sess-1")
    assert spec is not None
    env = mgr.env_for("sess-1")
    assert env["DISPLAY"] == ":100"
    assert env["XAUTHORITY"].endswith("sess-1.Xauthority") or "Xauthority" in env["XAUTHORITY"]
    assert any("Xvfb" in a[0] for a in spawned)

@pytest.mark.asyncio
async def test_start_display_returns_none_when_never_ready(tmp_path):
    async def fake_spawn(*argv, **kw): return FakeProc()
    mgr = X11Manager(DisplayAllocator(100, 1, 6100), resolution="800x600x24",
                     max_browsers=2, spawn=fake_spawn,
                     ready_check=lambda d: False, run_dir=str(tmp_path),
                     ready_timeout_s=0.2, ready_interval_s=0.05)
    spec = await mgr.start_display("sess-x")
    assert spec is None
    assert mgr.env_for("sess-x") == {}
    # lease was released — pool full capacity available again
    assert (await mgr._allocator.alloc()).display == 100

@pytest.mark.asyncio
async def test_stop_display_idempotent(tmp_path):
    async def fake_spawn(*argv, **kw): return FakeProc()
    mgr = X11Manager(DisplayAllocator(100, 4, 6100), resolution="800x600x24",
                     max_browsers=2, spawn=fake_spawn,
                     ready_check=lambda d: True, run_dir=str(tmp_path))
    await mgr.start_display("sess-1")
    await mgr.stop_display("sess-1")
    await mgr.stop_display("sess-1")  # no raise
    assert mgr.display_for("sess-1") is None

@pytest.mark.asyncio
async def test_ensure_vnc_spawns_x11vnc_once(tmp_path):
    spawned=[]
    async def fake_spawn(*argv, **kw): spawned.append(argv[0]); return FakeProc()
    mgr = X11Manager(DisplayAllocator(100,2,6100), resolution="800x600x24", max_browsers=2,
                     spawn=fake_spawn, ready_check=lambda d: True, run_dir=str(tmp_path))
    await mgr.start_display("S")
    p1 = await mgr.ensure_vnc("S"); p2 = await mgr.ensure_vnc("S")
    assert p1 == p2 == 6100
    assert spawned.count("x11vnc") == 1

@pytest.mark.asyncio
async def test_launch_browser_respects_cap(tmp_path):
    async def fake_spawn(*argv, **kw): return FakeProc()
    mgr = X11Manager(DisplayAllocator(100,2,6100), resolution="800x600x24", max_browsers=1,
                     spawn=fake_spawn, ready_check=lambda d: True, run_dir=str(tmp_path))
    await mgr.start_display("S")
    assert await mgr.launch_browser("S", "https://example.com") is True
    assert await mgr.launch_browser("S", None) is False  # cap=1

@pytest.mark.asyncio
async def test_xvfb_screen_geometry_comes_from_resolution(tmp_path):
    spawned = []
    async def fake_spawn(*argv, **kw):
        spawned.append(argv); return FakeProc()
    mgr = X11Manager(DisplayAllocator(100, 1, 6100), resolution="1280x800x24",
                     max_browsers=1, spawn=fake_spawn,
                     ready_check=lambda d: True, run_dir=str(tmp_path))
    await mgr.start_display("sess-geom")
    xvfb_argv = next(a for a in spawned if a[0] == "Xvfb")
    assert "1280x800x24" in xvfb_argv, xvfb_argv


@pytest.mark.asyncio
async def test_stop_all_terminates_every_display(tmp_path):
    """Shutdown sweep (issue #206): stop_all clears every tracked display."""
    async def fake_spawn(*argv, **kw): return FakeProc()
    mgr = X11Manager(DisplayAllocator(100, 4, 6100), resolution="800x600x24",
                     max_browsers=2, spawn=fake_spawn,
                     ready_check=lambda d: True, run_dir=str(tmp_path))
    await mgr.start_display("a")
    await mgr.start_display("b")
    assert mgr.display_for("a") is not None and mgr.display_for("b") is not None

    await mgr.stop_all()

    assert mgr.display_for("a") is None and mgr.display_for("b") is None
    assert mgr._specs == {}


@pytest.mark.asyncio
async def test_stop_all_resilient_to_one_failure(tmp_path):
    """One session's teardown raising must not abort the rest (return_exceptions)."""
    async def fake_spawn(*argv, **kw): return FakeProc()
    mgr = X11Manager(DisplayAllocator(100, 4, 6100), resolution="800x600x24",
                     max_browsers=2, spawn=fake_spawn,
                     ready_check=lambda d: True, run_dir=str(tmp_path))
    await mgr.start_display("a")
    await mgr.start_display("b")

    real_stop = mgr.stop_display
    async def flaky(sid):
        if sid == "a":
            raise RuntimeError("boom")
        await real_stop(sid)
    mgr.stop_display = flaky

    await mgr.stop_all()   # must not raise
    assert mgr.display_for("b") is None  # b cleaned despite a failing


# --- startup orphan-process sweep (issue #206, self-healing keystone) -------------

class _Proc:
    """Minimal /proc stand-in for sweep_orphan_processes (mirrors adopt.ProcInfo)."""
    def __init__(self, pid, cmdline, environ=None):
        self.pid = pid
        self.cmdline = cmdline
        self.environ = environ or {}


def _mgr(tmp_path):
    async def fake_spawn(*argv, **kw): return FakeProc()
    return X11Manager(DisplayAllocator(100, 4, 6100), resolution="800x600x24",
                      max_browsers=2, spawn=fake_spawn,
                      ready_check=lambda d: True, run_dir=str(tmp_path))


def test_sweep_orphan_kills_in_range_xvfb(tmp_path):
    """An Xvfb on a managed display with no live session is a leak → reaped."""
    mgr = _mgr(tmp_path)  # managed range 100..103, no live sessions
    killed_pids = []
    procs = [_Proc(111, ["Xvfb", ":101", "-screen", "0", "800x600x24"])]
    n = mgr.sweep_orphan_processes(lister=lambda: procs,
                                   killer=lambda pid: killed_pids.append(pid) or True)
    assert n == 1
    assert killed_pids == [111]


def test_sweep_orphan_spares_out_of_range_and_unrelated(tmp_path):
    """Displays outside the pool, and non-X processes, are never touched."""
    mgr = _mgr(tmp_path)  # range 100..103
    killed_pids = []
    procs = [
        _Proc(1, ["Xvfb", ":0"]),                       # out of range (user's display)
        _Proc(2, ["Xvfb", ":150"]),                     # out of range
        _Proc(3, ["python", "-m", "server.app"]),       # not an X process
        _Proc(4, ["chromium"], {"DISPLAY": ":7"}),      # chromium, out of range
    ]
    n = mgr.sweep_orphan_processes(lister=lambda: procs,
                                   killer=lambda pid: killed_pids.append(pid) or True)
    assert n == 0
    assert killed_pids == []


@pytest.mark.asyncio
async def test_sweep_orphan_spares_live_display(tmp_path):
    """A managed display that belongs to a LIVE session must not be reaped."""
    mgr = _mgr(tmp_path)
    await mgr.start_display("live")            # takes display :100
    assert mgr.display_for("live").lease.display == 100
    killed_pids = []
    procs = [_Proc(222, ["Xvfb", ":100"])]      # matches the live session's display
    n = mgr.sweep_orphan_processes(lister=lambda: procs,
                                   killer=lambda pid: killed_pids.append(pid) or True)
    assert n == 0
    assert killed_pids == []


def test_sweep_orphan_matches_x11vnc_and_chromium(tmp_path):
    """x11vnc (argv -display) and chromium (DISPLAY env) in range are both reaped."""
    mgr = _mgr(tmp_path)
    killed_pids = []
    procs = [
        _Proc(10, ["x11vnc", "-display", ":102", "-rfbport", "6102"]),
        _Proc(11, ["chromium", "--no-first-run"], {"DISPLAY": ":103"}),
    ]
    n = mgr.sweep_orphan_processes(lister=lambda: procs,
                                   killer=lambda pid: killed_pids.append(pid) or True)
    assert n == 2
    assert set(killed_pids) == {10, 11}


def test_sweep_orphan_counts_only_signalled(tmp_path):
    """A killer returning False (pid already gone) is not counted."""
    mgr = _mgr(tmp_path)
    procs = [_Proc(20, ["Xvfb", ":101"]), _Proc(21, ["Xvfb", ":102"])]
    n = mgr.sweep_orphan_processes(lister=lambda: procs,
                                   killer=lambda pid: pid == 20)
    assert n == 1


# --- CDP debug port + persistent profile (headless-reauth passkey flow) -----------

@pytest.mark.asyncio
async def test_launch_browser_no_debug_port_by_default(tmp_path):
    """Default call (no kwargs) must NOT include --remote-debugging-port; debug_port_for returns None."""
    spawned = []
    async def fake_spawn(*argv, **kw):
        spawned.append(argv)
        return FakeProc()
    mgr = X11Manager(DisplayAllocator(100, 2, 6100), resolution="800x600x24",
                     max_browsers=2, spawn=fake_spawn,
                     ready_check=lambda d: True, run_dir=str(tmp_path))
    await mgr.start_display("S")
    result = await mgr.launch_browser("S", "http://x")
    assert result is True
    chromium_argv = next(a for a in spawned if a[0] == "chromium")
    assert not any(arg.startswith("--remote-debugging-port") for arg in chromium_argv)
    assert mgr.debug_port_for("S") is None


@pytest.mark.asyncio
async def test_launch_browser_opt_in_debug_port(tmp_path):
    """debug_port=True: argv gets --remote-debugging-port=rfbport+1000 and --remote-allow-origins=*."""
    spawned = []
    async def fake_spawn(*argv, **kw):
        spawned.append(argv)
        return FakeProc()
    # DisplayAllocator(100, 2, 6100) → first display=100, rfbport=6100
    mgr = X11Manager(DisplayAllocator(100, 2, 6100), resolution="800x600x24",
                     max_browsers=2, spawn=fake_spawn,
                     ready_check=lambda d: True, run_dir=str(tmp_path))
    await mgr.start_display("S")
    result = await mgr.launch_browser("S", "http://x", debug_port=True)
    assert result is True
    chromium_argv = next(a for a in spawned if a[0] == "chromium")
    assert "--remote-debugging-port=7100" in chromium_argv
    assert "--remote-allow-origins=*" in chromium_argv
    assert mgr.debug_port_for("S") == 7100


@pytest.mark.asyncio
async def test_launch_browser_persistent_profile(tmp_path):
    """profile_dir not None: argv contains --user-data-dir={profile_dir}."""
    spawned = []
    async def fake_spawn(*argv, **kw):
        spawned.append(argv)
        return FakeProc()
    mgr = X11Manager(DisplayAllocator(100, 2, 6100), resolution="800x600x24",
                     max_browsers=2, spawn=fake_spawn,
                     ready_check=lambda d: True, run_dir=str(tmp_path))
    await mgr.start_display("S")
    result = await mgr.launch_browser("S", "http://x", profile_dir="/tmp/p")
    assert result is True
    chromium_argv = next(a for a in spawned if a[0] == "chromium")
    assert "--user-data-dir=/tmp/p" in chromium_argv
