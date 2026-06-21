import asyncio
from server.sessions.driver import SessionDriver
from server.sessions.permissions import PermissionRegistry
from server.sessions.events import TurnStart, TextDelta, TurnEnd, Status, Title, QueueUpdate, UserMessage
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
    # The user's prompt is echoed as a UserMessage before the assistant turn.
    assert [n for n in names if n not in ("Status", "Title")] == [
        "UserMessage", "TurnStart", "TextDelta", "TextDelta", "TurnEnd"]
    user_ev = next(e for e in seen if isinstance(e, UserMessage))
    assert user_ev.text == "hi"
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


def test_askuserquestion_blocks_emits_question_and_returns_deny_with_answer():
    """AskUserQuestion blocks the SDK callback, emits ev.Question, returns PermissionResultDeny
    whose message contains the user's chosen answer (the Deny channel feeds the answer to the model)."""
    from claude_agent_sdk import PermissionResultDeny
    from server.sessions.events import Question

    async def run():
        d = SessionDriver("sq", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=_factory([]), registry=PermissionRegistry(), identity="me")
        # Start the can_use_tool call in a background task — it will block until answered.
        tool_input = {"questions": [{"header": "Choose", "question": "Favourite?",
                                     "options": [{"label": "Option A", "description": ""},
                                                 {"label": "Option B", "description": ""}],
                                     "multiSelect": False}]}
        callback_task = asyncio.create_task(d._can_use_tool("AskUserQuestion", tool_input))

        # Give the callback time to emit the Question event and park on the future.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        # A Question event must have been placed in the output queue.
        assert not d._out.empty(), "expected ev.Question in output queue"
        q_event = d._out.get_nowait()
        assert isinstance(q_event, Question), f"expected Question, got {type(q_event)}"
        assert q_event.question == "Favourite?"
        assert q_event.header == "Choose"
        assert len(q_event.options) == 2
        assert q_event.options[0]["label"] == "Option A"
        assert q_event.multi_select is False

        # Resolve the question via driver.answer_question — same API as resolve_permission.
        answered = d.answer_question(q_event.question_id, ["Option A"], "me")
        assert answered is True

        # The callback should now return PermissionResultDeny with the formatted answer.
        result = await asyncio.wait_for(callback_task, timeout=2.0)
        assert isinstance(result, PermissionResultDeny)
        assert "Option A" in result.message
        assert "[User answered via the question UI]" in result.message

    asyncio.run(run())


def test_askuserquestion_dismiss_returns_deny_with_dismissed_text():
    """When a question is dismissed (deny_all), the callback returns PermissionResultDeny
    with a '(dismissed)' message so the model is informed."""
    from claude_agent_sdk import PermissionResultDeny
    import asyncio

    async def run():
        d = SessionDriver("sq-dismiss", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=_factory([]), registry=PermissionRegistry(), identity="me")
        tool_input = {"questions": [{"header": "H", "question": "Q?",
                                     "options": [{"label": "Yes"}], "multiSelect": False}]}
        callback_task = asyncio.create_task(d._can_use_tool("AskUserQuestion", tool_input))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        # Dismiss via interrupt (calls deny_all on the question registry).
        await d.interrupt()

        result = await asyncio.wait_for(callback_task, timeout=2.0)
        assert isinstance(result, PermissionResultDeny)
        assert "dismissed" in result.message.lower()

    asyncio.run(run())


def test_askuserquestion_multi_question_single_call():
    """A tool_input with multiple questions emits one Question event per question
    and formats all answers in the deny message."""
    from claude_agent_sdk import PermissionResultDeny
    from server.sessions.events import Question

    async def run():
        d = SessionDriver("sq-multi", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=_factory([]), registry=PermissionRegistry(), identity="me")
        tool_input = {
            "questions": [
                {"header": "H1", "question": "Q1?",
                 "options": [{"label": "Yes"}, {"label": "No"}], "multiSelect": False},
                {"header": "H2", "question": "Q2?",
                 "options": [{"label": "A"}, {"label": "B"}], "multiSelect": True},
            ]
        }
        callback_task = asyncio.create_task(d._can_use_tool("AskUserQuestion", tool_input))
        # Give enough ticks for both Question events to be emitted.
        for _ in range(4):
            await asyncio.sleep(0)

        # Collect Question events from the output queue.
        q_events = []
        while not d._out.empty():
            ev_item = d._out.get_nowait()
            if isinstance(ev_item, Question):
                q_events.append(ev_item)

        assert len(q_events) == 2, f"expected 2 Question events, got {len(q_events)}"
        qids = [q.question_id for q in q_events]

        # Answer both questions.
        d.answer_question(qids[0], ["Yes"], "me")
        d.answer_question(qids[1], ["A", "B"], "me")

        result = await asyncio.wait_for(callback_task, timeout=2.0)
        assert isinstance(result, PermissionResultDeny)
        assert "Q1?" in result.message
        assert "Yes" in result.message
        assert "Q2?" in result.message
        assert "A" in result.message
        assert "B" in result.message

    asyncio.run(run())


def test_askuserquestion_options_normalized_from_strings():
    """Options given as plain strings are normalized to dicts with a 'label' key."""
    from claude_agent_sdk import PermissionResultDeny
    from server.sessions.events import Question

    async def run():
        d = SessionDriver("sq-str-opts", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=_factory([]), registry=PermissionRegistry(), identity="me")
        tool_input = {"questions": [{"question": "Pick one",
                                     "options": ["Alpha", "Beta"], "multiSelect": False}]}
        callback_task = asyncio.create_task(d._can_use_tool("AskUserQuestion", tool_input))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert not d._out.empty()
        q_event = d._out.get_nowait()
        assert isinstance(q_event, Question)
        assert q_event.options[0] == {"label": "Alpha"}
        assert q_event.options[1] == {"label": "Beta"}

        d.answer_question(q_event.question_id, ["Alpha"], "me")
        result = await asyncio.wait_for(callback_task, timeout=2.0)
        assert isinstance(result, PermissionResultDeny)
        assert "Alpha" in result.message

    asyncio.run(run())


# ── QueueUpdate event + cancel_queued ─────────────────────────────────────────

def test_queue_update_emitted_when_enqueuing():
    """Enqueuing two prompts while busy should emit QueueUpdate events with the queue contents."""

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
        d = SessionDriver("qu1", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=factory, registry=PermissionRegistry(), identity="me")
        await d.start()
        await d.send_prompt("first")          # starts immediately (no QueueUpdate)
        await d.send_prompt("second")         # enqueued → QueueUpdate(["second"])
        await d.send_prompt("third")          # enqueued → QueueUpdate(["second","third"])

        # Drain what's in _out right now (non-blocking)
        events = []
        while not d._out.empty():
            events.append(d._out.get_nowait())

        await d.aclose()
        return events

    events = asyncio.run(run())
    queue_updates = [e for e in events if isinstance(e, QueueUpdate)]
    # Two enqueue operations → two QueueUpdate events
    assert len(queue_updates) == 2
    assert queue_updates[0].items == ["second"]
    assert queue_updates[1].items == ["second", "third"]


def test_cancel_queued_removes_item_and_emits_queue_update():
    """cancel_queued(0) removes the first queued item and emits a QueueUpdate + Status."""

    class SlowFakeSDK:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return False
        async def query(self, prompt: str): pass
        async def receive_response(self):
            await asyncio.sleep(0.2)
            yield f.result(uuid="t1")
        async def interrupt(self): pass
        async def set_permission_mode(self, mode): pass

    def factory(cwd, cfg, can_use_tool, mode, resume):
        return SlowFakeSDK()

    async def run():
        d = SessionDriver("qc1", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=factory, registry=PermissionRegistry(), identity="me")
        await d.start()
        await d.send_prompt("first")     # running
        await d.send_prompt("second")    # queued[0]
        await d.send_prompt("third")     # queued[1]
        assert d.queued() == ["second", "third"]

        # Drain enqueue events emitted so far
        while not d._out.empty():
            d._out.get_nowait()

        result = await d.cancel_queued(0)
        assert result is True
        assert d.queued() == ["third"]

        # The QueueUpdate and Status should have been emitted
        events = []
        while not d._out.empty():
            events.append(d._out.get_nowait())

        queue_updates = [e for e in events if isinstance(e, QueueUpdate)]
        statuses = [e for e in events if isinstance(e, Status)]
        assert len(queue_updates) == 1
        assert queue_updates[0].items == ["third"]
        assert any(s.count == 1 for s in statuses)

        await d.aclose()

    asyncio.run(run())


def test_cancel_queued_out_of_range_returns_false():
    """cancel_queued with an out-of-range index returns False without emitting events."""

    class SlowFakeSDK:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return False
        async def query(self, prompt: str): pass
        async def receive_response(self):
            await asyncio.sleep(0.2)
            yield f.result(uuid="t1")
        async def interrupt(self): pass
        async def set_permission_mode(self, mode): pass

    def factory(cwd, cfg, can_use_tool, mode, resume):
        return SlowFakeSDK()

    async def run():
        d = SessionDriver("qc2", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=factory, registry=PermissionRegistry(), identity="me")
        await d.start()
        await d.send_prompt("first")
        await d.send_prompt("second")
        # Drain setup events
        while not d._out.empty():
            d._out.get_nowait()

        result_neg = await d.cancel_queued(-1)
        result_oob = await d.cancel_queued(5)
        assert result_neg is False
        assert result_oob is False
        # No events emitted for invalid indices
        assert d._out.empty()

        await d.aclose()

    asyncio.run(run())


def test_on_status_fires_in_progress_then_done():
    """The driver notifies the async on_status(session_id, derived_status) callback —
    'in_progress' when a turn starts and 'done' when it finishes — so the orchestrator
    can keep the session LIST's (rich) status in sync with the driver's real state."""
    msgs = [f.stream_start(uuid="t1"), f.stream_text("hi", uuid="t1"),
            f.assistant(TextBlock(text="hi"), uuid="t1"), f.result(uuid="t1")]
    seen: list[str] = []
    async def on_status(session_id, status):
        seen.append(status)
    async def run():
        d = SessionDriver("sx", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=_factory(msgs), registry=PermissionRegistry(),
                          identity="me", on_status=on_status)
        await d.start()
        await d.send_prompt("hi")
        async for e in d.events():
            if isinstance(e, Status) and e.state == "idle":
                break
        if d._pump:
            await d._pump
        await d.aclose()
    asyncio.run(run())
    assert "in_progress" in seen, f"expected an 'in_progress' status, got {seen}"
    assert seen and seen[-1] == "done", f"expected to end 'done', got {seen}"
