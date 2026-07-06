import asyncio
from server.sessions import copilot_proxy as cp


class FakeProc:
    def __init__(self): self.returncode = None; self.terminated = False
    def terminate(self): self.terminated = True; self.returncode = 0
    def kill(self): self.returncode = -9
    async def wait(self): return self.returncode


def _mgr(ready_check=None, **kw):
    procs = []
    async def spawn(*argv, **kwargs):
        p = FakeProc(); procs.append((argv, p)); return p
    if ready_check is None:
        def ready_check(port):
            return True
    mgr = cp.CopilotProxyManager(
        cp.ProxyPortAllocator(4142, 4),
        token_dir_for=lambda a: f"/tmp/tok/{a}",
        spawn=spawn, ready_check=ready_check, liveness_s=0.0, **kw)
    return mgr, procs


def test_ensure_spawns_health_gated_and_is_idempotent(monkeypatch):
    monkeypatch.setattr(cp, "resolve_copilot_proxy", lambda: "/usr/bin/copilot2api")

    async def go():
        mgr, procs = _mgr()
        port = await mgr.ensure("c1")
        assert port == 4142
        argv, _ = procs[0]
        assert "copilot2api" in argv[0] or argv[0].endswith("copilot2api")
        assert "-port" in argv and "4142" in argv and "-token-dir" in argv
        assert await mgr.ensure("c1") == 4142      # idempotent, no 2nd spawn
        assert len(procs) == 1
        assert mgr.env_for("c1")["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:4142"
    asyncio.run(go())


def test_ensure_raises_when_never_ready(monkeypatch):
    monkeypatch.setattr(cp, "resolve_copilot_proxy", lambda: "/usr/bin/copilot2api")

    async def go():
        mgr, procs = _mgr(ready_check=lambda port: False, ready_timeout_s=0.1)
        try:
            await mgr.ensure("c1")
            assert False, "expected ProxyStartError"
        except cp.ProxyStartError:
            pass
        assert procs[0][1].terminated  # cleaned up
    asyncio.run(go())
