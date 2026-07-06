import asyncio
from server.sessions import copilot_proxy as cp


def test_resolve_prefers_env_override(monkeypatch, tmp_path):
    b = tmp_path / "copilot2api"; b.write_text("#!/bin/sh\n"); b.chmod(0o755)
    monkeypatch.setenv("CCSWITCH_COPILOT_PROXY_BIN", str(b))
    assert cp.resolve_copilot_proxy() == str(b)


def test_resolve_none_when_absent(monkeypatch):
    monkeypatch.delenv("CCSWITCH_COPILOT_PROXY_BIN", raising=False)
    monkeypatch.setattr(cp.shutil, "which", lambda _: None)
    assert cp.resolve_copilot_proxy() is None


def test_port_allocator_roundtrips():
    async def go():
        a = cp.ProxyPortAllocator(4142, 2)
        p1 = await a.alloc(); p2 = await a.alloc()
        assert {p1, p2} == {4142, 4143}
        await a.release(p1)
        assert await a.alloc() == p1
    asyncio.run(go())
