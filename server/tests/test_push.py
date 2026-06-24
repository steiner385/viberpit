import json
import pytest
from server.sessions import push as push_mod

@pytest.mark.asyncio
async def test_push_noop_without_url():
    assert await push_mod.push("t", "b", url=None) is False

@pytest.mark.asyncio
async def test_push_posts_json(monkeypatch):
    seen = {}
    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b""
    def opener(req, timeout=0):
        seen["url"] = req.full_url
        seen["body"] = json.loads(req.data.decode())
        return _Resp()
    ok = await push_mod.push("Needs you", "session X", url="https://ntfy.sh/mytopic", opener=opener)
    assert ok is True
    assert seen["url"] == "https://ntfy.sh/mytopic"
    assert seen["body"] == {"title": "Needs you", "body": "session X"}

@pytest.mark.asyncio
async def test_push_never_raises_on_error(monkeypatch):
    def opener(req, timeout=0): raise OSError("down")
    assert await push_mod.push("t", "b", url="https://x", opener=opener) is False
