# server/tests/test_x11_manager.py
import asyncio
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
