import asyncio
from server.sessions.driver import SessionDriver
from server.sessions.permissions import PermissionRegistry
from server.sessions.events import TurnStart, TextDelta, TurnEnd, Status, Title
from server.tests.fakes.fake_sdk import FakeSDKClient
from server.tests.fakes import fake_sdk as f
from claude_agent_sdk import TextBlock

def _factory(msgs):
    def make(cwd, config_dir, can_use_tool, mode, resume):
        return FakeSDKClient(msgs, on_can_use_tool=can_use_tool)
    return make

def test_driver_streams_turn_and_sets_session_id():
    msgs = [f.sys_init("sess-1"), f.stream_start(uuid="t1"),
            f.stream_text("Hello ", uuid="t1"), f.stream_text("there", uuid="t1"),
            f.assistant(TextBlock(text="Hello there"), uuid="t1"), f.result(session_id="sess-1", uuid="t1")]
    async def run():
        d = SessionDriver("s1", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=_factory(msgs), registry=PermissionRegistry(), identity="me")
        await d.start()
        await d.send_prompt("hi")
        seen = []
        async for e in d.events():
            seen.append(e)
            if isinstance(e, TurnEnd):
                break
        await d.aclose()
        return seen, d.claude_session_id
    seen, sid = asyncio.run(run())
    names = [type(e).__name__ for e in seen]
    # C3: first event is now Title (auto-title from first prompt)
    assert names[0] == "Title"
    assert names[1] == "Status"  # working
    assert [n for n in names if n not in ("Status", "Title")] == ["TurnStart", "TextDelta", "TextDelta", "TurnEnd"]
    assert sid == "sess-1"

def test_interrupt_denies_pending_and_stops():
    msgs = [f.stream_start(uuid="t1"), f.stream_text("a", uuid="t1"), f.result(uuid="t1")]
    async def run():
        reg = PermissionRegistry()
        d = SessionDriver("s2", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=_factory(msgs), registry=reg, identity="me")
        await d.start()
        await d.send_prompt("go")
        await d.interrupt()
        # a pending permission for this session would now be denied
        pid, fut = await reg.request("s2", "t1", "Bash", {"command": "ls"}, mode="interactive",
                                     denylist=PermissionRegistry.DANGEROUS, allowlist=True, identity="me")
        reg.deny_all("s2")
        await d.aclose()
        return await fut
    assert asyncio.run(run()) is False

def test_events_terminates_after_aclose():
    msgs = [f.result(uuid="t1")]
    async def run():
        d = SessionDriver("s3", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=_factory(msgs), registry=PermissionRegistry(), identity="me")
        await d.start(); await d.send_prompt("x")
        got = []
        async def consume():
            async for e in d.events():
                got.append(e)
        task = asyncio.create_task(consume())
        await asyncio.sleep(0.05)
        await d.aclose()
        await asyncio.wait_for(task, timeout=2)   # must terminate, not hang
        return True
    assert asyncio.run(run()) is True


# ── C2: FIFO queue while busy ─────────────────────────────────────────────────

def test_concurrent_send_prompt_enqueues():
    """Calling send_prompt a second time while drain is running ENQUEUES the
    prompt (does not reject it or start a second pump immediately)."""
    import asyncio

    # Use a slow fake SDK: yield results only after a short delay so the first
    # drain is still running when the second send_prompt fires.
    class SlowFakeSDK:
        def __init__(self):
            self.queries: list[str] = []
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return False
        async def query(self, prompt: str): self.queries.append(prompt)
        async def receive_response(self):
            await asyncio.sleep(0.05)   # keep drain alive long enough
            yield f.result(uuid="t1")
        async def interrupt(self): pass
        async def set_permission_mode(self, mode): pass

    sdk = SlowFakeSDK()

    def factory(cwd, cfg, can_use_tool, mode, resume):
        return sdk

    async def run():
        d = SessionDriver("s4", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=factory, registry=PermissionRegistry(), identity="me")
        await d.start()
        await d.send_prompt("first")
        # At this point pump is running (drain is sleeping).
        pump_after_first = d._pump
        assert pump_after_first is not None
        assert not pump_after_first.done()
        # Second send_prompt must enqueue, not create a second pump.
        await d.send_prompt("second")
        pump_after_second = d._pump
        # Same task object — no second pump was created yet.
        assert pump_after_second is pump_after_first
        # The second prompt is in the queue.
        assert d.queued() == ["second"]
        await d.aclose()
        # Only one query was actually sent (second is abandoned by aclose cancel).
        return sdk.queries

    queries = asyncio.run(run())
    assert queries == ["first"]


def test_queued_prompt_runs_after_first_turn():
    """After the first turn drains, the queued prompt is sent automatically.
    Both prompts end up in sdk.queries in order. Only the final turn emits idle."""
    import asyncio

    # SDK that sends responses immediately (no delay), so both turns can complete.
    class QuickFakeSDK:
        def __init__(self):
            self.queries: list[str] = []
            self._call = 0
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return False
        async def query(self, prompt: str): self.queries.append(prompt)
        async def receive_response(self):
            self._call += 1
            yield f.result(uuid=f"t{self._call}")
        async def interrupt(self): pass
        async def set_permission_mode(self, mode): pass

    sdk = QuickFakeSDK()

    def factory(cwd, cfg, can_use_tool, mode, resume):
        return sdk

    async def run():
        d = SessionDriver("sq1", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=factory, registry=PermissionRegistry(), identity="me")
        await d.start()
        # Send both prompts back-to-back; the first starts immediately, the second queues.
        await d.send_prompt("prompt-one")
        await d.send_prompt("prompt-two")
        # Collect events until we see the final idle status (emitted after turn 2).
        # Turn 1 drain → start turn 2 (no idle). Turn 2 drain → idle.
        async for e in d.events():
            if isinstance(e, Status) and e.state == "idle":
                break
        await d.aclose()
        return sdk.queries

    queries = asyncio.run(run())
    assert queries == ["prompt-one", "prompt-two"]


def test_interrupt_clears_queue():
    """interrupt() clears the queue and emits an idle status."""
    import asyncio

    class SlowFakeSDK:
        def __init__(self):
            self.queries: list[str] = []
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return False
        async def query(self, prompt: str): self.queries.append(prompt)
        async def receive_response(self):
            await asyncio.sleep(0.1)
            yield f.result(uuid="t1")
        async def interrupt(self): pass
        async def set_permission_mode(self, mode): pass

    sdk = SlowFakeSDK()

    def factory(cwd, cfg, can_use_tool, mode, resume):
        return sdk

    async def run():
        d = SessionDriver("sq2", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=factory, registry=PermissionRegistry(), identity="me")
        await d.start()
        await d.send_prompt("first")
        # Queue a second prompt while first is running.
        await d.send_prompt("queued-one")
        await d.send_prompt("queued-two")
        assert len(d.queued()) == 2
        # Interrupt — should clear the queue.
        await d.interrupt()
        assert d.queued() == []
        await d.aclose()

    asyncio.run(run())


# ── C3: auto-title from first prompt ────────────────────────────────────────

def test_auto_title_emitted_on_first_prompt():
    """The first send_prompt should emit a Title event with the (truncated) text."""
    msgs = [f.result(uuid="t1")]

    async def run():
        d = SessionDriver("s5", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=_factory(msgs), registry=PermissionRegistry(), identity="me")
        await d.start()
        long_prompt = "A" * 60 + " extra stuff"
        await d.send_prompt(long_prompt)
        events = []
        async for e in d.events():
            events.append(e)
            if isinstance(e, Status) and e.state == "idle":
                break
        await d.aclose()
        return events

    events = asyncio.run(run())
    title_events = [e for e in events if isinstance(e, Title)]
    assert len(title_events) == 1
    t = title_events[0]
    # First line, truncated to 50 chars
    assert t.title == "A" * 50
    assert t.source == "prompt"


def test_auto_title_only_fires_once():
    """Title should only be emitted on the first prompt, not subsequent ones."""
    # We need a driver that can handle multiple send_prompt calls sequentially.
    # Use a fresh SDK per prompt by using a list of message-lists.

    call_count = 0

    class MultiCallSDK:
        def __init__(self):
            self.sent: list[str] = []
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return False
        async def query(self, prompt: str): self.sent.append(prompt)
        async def receive_response(self):
            yield f.result(uuid=f"t{len(self.sent)}")
        async def interrupt(self): pass
        async def set_permission_mode(self, mode): pass

    sdk = MultiCallSDK()

    def factory(cwd, cfg, can_use_tool, mode, resume):
        return sdk

    async def run():
        d = SessionDriver("s6", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=factory, registry=PermissionRegistry(), identity="me")
        await d.start()
        all_events = []
        # First prompt: should emit Title
        await d.send_prompt("hello world")
        async for e in d.events():
            all_events.append(e)
            if isinstance(e, Status) and e.state == "idle":
                break
        # Second prompt: should NOT emit another Title
        await d.send_prompt("second query")
        async for e in d.events():
            all_events.append(e)
            if isinstance(e, Status) and e.state == "idle":
                break
        await d.aclose()
        return all_events

    events = asyncio.run(run())
    title_events = [e for e in events if isinstance(e, Title)]
    assert len(title_events) == 1
    assert title_events[0].title == "hello world"


def test_auto_title_first_line_only():
    """Title is derived from the first line of the prompt only."""
    msgs = [f.result(uuid="t1")]

    async def run():
        d = SessionDriver("s7", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=_factory(msgs), registry=PermissionRegistry(), identity="me")
        await d.start()
        await d.send_prompt("First line\nSecond line that should be ignored")
        events = []
        async for e in d.events():
            events.append(e)
            if isinstance(e, Status) and e.state == "idle":
                break
        await d.aclose()
        return events

    events = asyncio.run(run())
    title_events = [e for e in events if isinstance(e, Title)]
    assert len(title_events) == 1
    assert title_events[0].title == "First line"
