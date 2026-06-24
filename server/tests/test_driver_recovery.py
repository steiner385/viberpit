"""Driver surfaces recoverable turn failures (429/401) via on_recoverable_failure."""
import asyncio
from claude_agent_sdk import RateLimitEvent, RateLimitInfo
from server.sessions.driver import SessionDriver
from server.sessions.permissions import PermissionRegistry
from server.sessions import events as ev
from server.tests.fakes.fake_sdk import FakeSDKClient
from server.tests.fakes import fake_sdk as f


def _factory(msgs):
    def make(cwd, config_dir, can_use_tool, mode, resume, *, on_pre_compact=None):
        return FakeSDKClient(msgs, on_can_use_tool=can_use_tool)
    return make


def _rate_limit(resets_at):
    info = RateLimitInfo(status="rejected", resets_at=resets_at, rate_limit_type="five_hour",
                         utilization=1.0, overage_status=None, overage_resets_at=None,
                         overage_disabled_reason=None, raw={})
    return RateLimitEvent(rate_limit_info=info, uuid="u", session_id="sess-1")


def _run(msgs, *, clock=None):
    calls = []
    async def on_fail(session_id, kind, retry_after):
        calls.append((session_id, kind, retry_after))
    async def go():
        d = SessionDriver("s1", "kindash", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=_factory(msgs), registry=PermissionRegistry(),
                          identity="me", on_recoverable_failure=on_fail, clock=clock)
        await d.start()
        await d.send_prompt("go")
        seen = []
        async for e in d.events():
            seen.append(e)
            if isinstance(e, ev.TurnEnd):
                break
        await d.aclose()
        return seen
    seen = asyncio.run(go())
    return seen, calls


def test_429_result_invokes_recovery_callback_and_emits_turn_failure():
    msgs = [f.sys_init("sess-1"), f.stream_start(uuid="t1"),
            f.result(session_id="sess-1", uuid="t1", is_error=True, api_error_status=429)]
    seen, calls = _run(msgs)
    assert calls and calls[0][0] == "s1" and calls[0][1] == "rate_limit"
    tf = [e for e in seen if isinstance(e, ev.TurnFailure)]
    assert tf and tf[0].kind == "rate_limit" and tf[0].account == "kindash"


def test_401_result_invokes_recovery_callback_as_auth():
    msgs = [f.sys_init("sess-1"), f.stream_start(uuid="t1"),
            f.result(session_id="sess-1", uuid="t1", is_error=True, api_error_status=401)]
    _, calls = _run(msgs)
    assert calls and calls[0][1] == "auth_401"


def test_retry_after_derived_from_rate_limit_reset():
    # RateLimitEvent observed mid-turn carries resets_at; with clock fixed at 1000,
    # a reset at 1012 yields retry_after == 12.
    msgs = [f.sys_init("sess-1"), f.stream_start(uuid="t1"),
            _rate_limit(resets_at=1012),
            f.result(session_id="sess-1", uuid="t1", is_error=True, api_error_status=429)]
    _, calls = _run(msgs, clock=lambda: 1000.0)
    assert calls and calls[0][1] == "rate_limit"
    assert calls[0][2] == 12.0


def test_successful_turn_does_not_invoke_callback():
    from claude_agent_sdk import TextBlock
    msgs = [f.sys_init("sess-1"), f.stream_start(uuid="t1"),
            f.assistant(TextBlock(text="ok"), uuid="t1"), f.result(session_id="sess-1", uuid="t1")]
    _, calls = _run(msgs)
    assert calls == []


def test_drain_exception_with_401_is_classified_auth():
    class _Raising:
        async def __aenter__(self): return self
        async def __aexit__(self, *e): return False
        async def query(self, p): pass
        async def receive_response(self):
            raise RuntimeError("APIError: 401 Invalid authentication credentials")
            yield
        async def interrupt(self): pass
        async def set_permission_mode(self, m): pass
    def factory(cwd, config_dir, can_use_tool, mode, resume, *, on_pre_compact=None):
        return _Raising()
    calls = []
    async def on_fail(session_id, kind, retry_after):
        calls.append((session_id, kind, retry_after))
    async def go():
        d = SessionDriver("s1", "kindash", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=factory, registry=PermissionRegistry(), identity="me",
                          on_recoverable_failure=on_fail)
        await d.start(); await d.send_prompt("go")
        async for e in d.events():
            if isinstance(e, ev.ErrorEvent):
                break
        await d.aclose()
    asyncio.run(go())
    assert calls and calls[0][1] == "auth_401"


def test_subprocess_crash_signals_process_crash():
    calls = []
    async def on_fail(session_id, kind, retry_after):
        calls.append((session_id, kind, retry_after))

    class _CrashingClient(FakeSDKClient):
        async def receive_response(self):
            raise RuntimeError("Command failed with exit code -6")
            yield  # unreachable — makes this an async generator

    async def go():
        d = SessionDriver("s1", "kindash", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=lambda *a, **k: _CrashingClient([]),
                          registry=PermissionRegistry(), identity="me",
                          on_recoverable_failure=on_fail)
        await d.start()
        await d.send_prompt("go")
        await d._pump          # wait for the drain task to fail
        await d.aclose()
    asyncio.run(go())
    assert ("s1", "process_crash", None) in calls
