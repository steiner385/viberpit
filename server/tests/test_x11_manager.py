# server/tests/test_x11_manager.py
import asyncio
import pytest
from server.sessions.x11manager import DisplayAllocator, X11Manager

class FakeProc:
    def __init__(self): self.pid = 4242; self._terminated = False; self.returncode = None
    def terminate(self): self._terminated = True; self.returncode = 0
    def kill(self): self.returncode = -9
    async def wait(self): return self.returncode if self.returncode is not None else 0


class HangingProc:
    """Ignores SIGTERM and never exits on its own — forces the shutdown budget to
    elapse so the hard-kill fallback is exercised. `kill()` is the only thing that
    sets a returncode."""
    def __init__(self): self.pid = 9999; self.returncode = None; self.killed = False
    def terminate(self): pass  # swallow SIGTERM
    def kill(self): self.killed = True; self.returncode = -9
    async def wait(self):
        if self.returncode is not None:
            return self.returncode
        await asyncio.sleep(3600)  # hang until cancelled
        return self.returncode

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
async def test_shutdown_all_tears_down_every_display(tmp_path):
    # Concurrent teardown: all live displays are stopped and the spec table emptied.
    async def fake_spawn(*argv, **kw): return FakeProc()
    mgr = X11Manager(DisplayAllocator(100, 4, 6100), resolution="800x600x24",
                     max_browsers=2, spawn=fake_spawn,
                     ready_check=lambda d: True, run_dir=str(tmp_path))
    for sid in ("a", "b", "c"):
        await mgr.start_display(sid)
    xvfbs = [mgr.display_for(s).xvfb for s in ("a", "b", "c")]
    n = await mgr.shutdown_all(timeout=5.0)
    assert n == 3
    assert all(mgr.display_for(s) is None for s in ("a", "b", "c"))
    assert all(p._terminated for p in xvfbs)  # each Xvfb got SIGTERM


@pytest.mark.asyncio
async def test_shutdown_all_empty_is_noop(tmp_path):
    async def fake_spawn(*argv, **kw): return FakeProc()
    mgr = X11Manager(DisplayAllocator(100, 1, 6100), resolution="800x600x24",
                     max_browsers=1, spawn=fake_spawn,
                     ready_check=lambda d: True, run_dir=str(tmp_path))
    assert await mgr.shutdown_all(timeout=5.0) == 0


@pytest.mark.asyncio
async def test_shutdown_all_hard_kills_when_budget_exceeded(tmp_path):
    # A display whose Xvfb ignores SIGTERM must be SIGKILLed by the fallback,
    # and shutdown_all must return promptly (bounded by the budget, not the 5s
    # per-process terminate wait × N).
    async def fake_spawn(*argv, **kw): return FakeProc()
    mgr = X11Manager(DisplayAllocator(100, 2, 6100), resolution="800x600x24",
                     max_browsers=1, spawn=fake_spawn,
                     ready_check=lambda d: True, run_dir=str(tmp_path))
    await mgr.start_display("stuck")
    hang = HangingProc()
    mgr.display_for("stuck").xvfb = hang  # replace with a SIGTERM-ignoring proc
    loop = asyncio.get_event_loop()
    t0 = loop.time()
    await mgr.shutdown_all(timeout=0.1)
    elapsed = loop.time() - t0
    assert hang.killed is True            # hard-killed by the fallback
    assert elapsed < 2.0                  # returned on the budget, not the 5s terminate wait
    assert mgr.display_for("stuck") is None
