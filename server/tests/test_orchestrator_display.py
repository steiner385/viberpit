# server/tests/test_orchestrator_display.py
import pytest
from server.sessions.orchestrator import SessionOrchestrator


class FakeX11:
    def __init__(self): self.started=[]; self.stopped=[]; self._env={}
    async def start_display(self, sid): self.started.append(sid); self._env[sid]={"DISPLAY":":100","XAUTHORITY":"/x"}; return object()
    def env_for(self, sid): return self._env.get(sid, {})
    def display_for(self, sid): return object() if sid in self._env else None
    async def stop_display(self, sid): self.stopped.append(sid); self._env.pop(sid, None)


@pytest.mark.asyncio
async def test_factory_closure_injects_display(monkeypatch):
    x11 = FakeX11()
    seen = {}
    def fake_factory(cwd, cfg, cut, mode, resume, extra_env=None, *, on_pre_compact=None):
        seen["extra_env"] = extra_env
        class C:  # minimal fake SDK client
            async def connect(self,*a,**k): ...
            async def disconnect(self): ...
        return C()
    orch = SessionOrchestrator(sdk_factory=fake_factory, x11=x11)
    await x11.start_display("S")  # simulate create-time allocation
    closure = orch._session_factory("S")
    closure("/cwd", "/cfg", None, "interactive", None)
    assert seen["extra_env"] == {"DISPLAY": ":100", "XAUTHORITY": "/x"}


@pytest.mark.asyncio
async def test_factory_closure_noop_without_x11():
    seen = {}
    def fake_factory(cwd, cfg, cut, mode, resume, extra_env=None, *, on_pre_compact=None):
        seen["extra_env"] = extra_env; return object()
    orch = SessionOrchestrator(sdk_factory=fake_factory, x11=None)
    orch._session_factory("S")("/c","/cfg",None,"interactive",None)
    assert seen["extra_env"] in (None, {})


@pytest.mark.asyncio
async def test_close_session_tears_down_display(tmp_path):
    from server.db import init_db
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"
    await init_db(db_url=db_url)

    x11 = FakeX11()
    orch = SessionOrchestrator(sdk_factory=lambda *a, **k: None, x11=x11)
    await x11.start_display("S")
    # close_session must call stop_display even if no driver registered
    await orch.close_session("S")
    assert "S" in x11.stopped
