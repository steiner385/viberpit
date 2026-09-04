import asyncio
from server.sessions.driver import SessionDriver
from server.sessions.permissions import PermissionRegistry
from server.sessions.events import TurnEnd, Status, Title, QueueUpdate, UserMessage, ErrorEvent
import server.sessions.events as ev
from server.tests.fakes.fake_sdk import FakeSDKClient
from server.tests.fakes import fake_sdk as f
from claude_agent_sdk import TextBlock
from server.tests.fakes.fake_sdk import PerQueryScript


class _RaisingClient(PerQueryScript):
    """SDK client whose stream raises partway through, to exercise _drain's except."""
    def __init__(self, exc):
        self._exc = exc
    async def __aenter__(self): return self
    async def __aexit__(self, *exc): return False
    async def query(self, prompt): pass
    async def receive_response(self):
        raise self._exc
        yield  # pragma: no cover — makes this an async generator
    async def interrupt(self): pass
    async def set_permission_mode(self, mode): pass


def test_drain_exception_emits_error_event_with_detail():
    """A crash in the SDK stream must reach the UI as an ErrorEvent that names the
    real failure — not a generic 'session stream failed' with the cause only logged."""
    def factory(cwd, config_dir, can_use_tool, mode, resume, *, on_pre_compact=None):
        return _RaisingClient(ValueError("token expired mid-stream"))

    async def run():
        d = SessionDriver("se", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=factory, registry=PermissionRegistry(), identity="me")
        await d.start()
        await d.send_prompt("go")
        seen = []
        async for e in d.events():
            seen.append(e)
            if isinstance(e, ErrorEvent):
                break
        await d.aclose()
        return seen
    seen = asyncio.run(run())
    err = next(e for e in seen if isinstance(e, ErrorEvent))
    assert err.code == "driver_error"
    assert "ValueError" in err.message
    assert "token expired mid-stream" in err.message


class _ModeRecorder:
    """Minimal SDK client stand-in that records set_permission_mode calls."""
    def __init__(self):
        self.modes = []
    async def set_permission_mode(self, mode):
        self.modes.append(mode)


def test_exiting_plan_restores_pre_plan_permission_mode():
    """The web plan toggle sends the 'default' sentinel to turn plan OFF. Exiting
    plan must return the session to the mode it had BEFORE plan (here full-auto),
    NOT collapse to the SDK 'default' engine — which the registry treats as
    "prompt on every dangerous tool", silently breaking a full-auto session."""
    async def run():
        rec = _ModeRecorder()
        d = SessionDriver("sp-plan", "primary", "/tmp/r", "/tmp/cfg", mode="full-auto",
                          sdk_factory=lambda *a, **k: None, registry=PermissionRegistry(),
                          identity="me")
        d._client = rec
        await d.set_permission_mode("plan")      # toggle plan ON
        assert d.mode == "plan"
        await d.set_permission_mode("default")   # toggle plan OFF (UI sentinel)
        return d
    d = asyncio.run(run())
    # The ENFORCED ccswitch mode (what PermissionRegistry.request reads) is back to
    # full-auto, so tools auto-approve again — not 'default', which would prompt.
    assert d.mode == "full-auto"


def test_default_outside_plan_is_not_hijacked():
    """A 'default' that does NOT follow plan is a real target and must pass through
    unchanged — the restore only fires when actually exiting plan."""
    async def run():
        rec = _ModeRecorder()
        d = SessionDriver("sp-nodef", "primary", "/tmp/r", "/tmp/cfg", mode="full-auto",
                          sdk_factory=lambda *a, **k: None, registry=PermissionRegistry(),
                          identity="me")
        d._client = rec
        await d.set_permission_mode("default")   # not exiting plan → literal 'default'
        return d
    d = asyncio.run(run())
    assert d.mode == "default"


def _factory(msgs):
    def make(cwd, config_dir, can_use_tool, mode, resume, extra_env=None, *, on_pre_compact=None):
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
        # Subscribe BEFORE send_prompt so control events (Title, Status) emitted at
        # turn start are captured — a late subscriber only replays transcript content.
        out_q: asyncio.Queue = asyncio.Queue()
        d._subscribers.add(out_q)
        await d.send_prompt("hi")
        if d._pump:
            await d._pump
        seen = []
        while not out_q.empty():
            e = out_q.get_nowait()
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
    # The assistant message emits a TurnModel event (its producing model) between
    # the streamed text deltas and TurnEnd.
    assert [n for n in names if n not in ("Status", "Title")] == [
        "UserMessage", "TurnStart", "TextDelta", "TextDelta", "TurnModel", "TurnEnd"]
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
    class SlowFakeSDK(PerQueryScript):
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

    def factory(cwd, cfg, can_use_tool, mode, resume, *, on_pre_compact=None):
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
    class QuickFakeSDK(PerQueryScript):
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

    def factory(cwd, cfg, can_use_tool, mode, resume, *, on_pre_compact=None):
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
            if isinstance(e, Status) and e.state in ("idle", "done", "complete", "needs_attention"):
                break
        await d.aclose()
        return sdk.queries

    queries = asyncio.run(run())
    assert queries == ["prompt-one", "prompt-two"]


def test_interrupt_clears_queue():
    """interrupt() clears the queue and emits an idle status."""
    import asyncio

    class SlowFakeSDK(PerQueryScript):
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

    def factory(cwd, cfg, can_use_tool, mode, resume, *, on_pre_compact=None):
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
        out_q: asyncio.Queue = asyncio.Queue()
        d._subscribers.add(out_q)
        long_prompt = "A" * 60 + " extra stuff"
        await d.send_prompt(long_prompt)
        if d._pump:
            await d._pump
        events = []
        while not out_q.empty():
            e = out_q.get_nowait()
            events.append(e)
            if isinstance(e, Status) and e.state in ("idle", "done", "complete", "needs_attention"):
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

    class MultiCallSDK(PerQueryScript):
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

    def factory(cwd, cfg, can_use_tool, mode, resume, *, on_pre_compact=None):
        return sdk

    async def run():
        d = SessionDriver("s6", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=factory, registry=PermissionRegistry(), identity="me")
        await d.start()
        out_q: asyncio.Queue = asyncio.Queue()
        d._subscribers.add(out_q)
        # First prompt: should emit Title
        await d.send_prompt("hello world")
        if d._pump:
            await d._pump
        # Second prompt: should NOT emit another Title
        await d.send_prompt("second query")
        if d._pump:
            await d._pump
        all_events = []
        while not out_q.empty():
            all_events.append(out_q.get_nowait())
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
        out_q: asyncio.Queue = asyncio.Queue()
        d._subscribers.add(out_q)
        await d.send_prompt("First line\nSecond line that should be ignored")
        if d._pump:
            await d._pump
        events = []
        while not out_q.empty():
            e = out_q.get_nowait()
            events.append(e)
            if isinstance(e, Status) and e.state in ("idle", "done", "complete", "needs_attention"):
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
        out_q: asyncio.Queue = asyncio.Queue()
        d._subscribers.add(out_q)
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
        assert not out_q.empty(), "expected ev.Question in output queue"
        q_event = out_q.get_nowait()
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


def test_permission_decision_emits_permission_resolved():
    """Deciding a permission emits ev.PermissionResolved through the driver fan-out.

    Same shape as QuestionResolved: the daemon ring replays the original
    PermissionRequest frame on a mid-turn reattach, so without a resolution frame
    in the same stream an already-decided permission resurrects in the pinned
    PendingPermissionPanel of every reattached client."""
    from claude_agent_sdk import PermissionResultAllow
    from server.sessions.events import PermissionRequest, PermissionResolved

    async def run():
        d = SessionDriver("sp-resolved", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=_factory([]), registry=PermissionRegistry(), identity="me")
        out_q: asyncio.Queue = asyncio.Queue()
        d._subscribers.add(out_q)
        callback_task = asyncio.create_task(d._can_use_tool("Bash", {"command": "ls"}))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        req = None
        while not out_q.empty():
            e = out_q.get_nowait()
            if isinstance(e, PermissionRequest):
                req = e
        assert req is not None, "expected a PermissionRequest event"

        assert d.resolve_permission(req.perm_id, "allow", "me") is True
        result = await asyncio.wait_for(callback_task, timeout=2.0)
        assert isinstance(result, PermissionResultAllow)

        resolved = []
        while not out_q.empty():
            e = out_q.get_nowait()
            if isinstance(e, PermissionResolved):
                resolved.append(e)
        assert len(resolved) == 1, f"expected exactly one PermissionResolved, got {len(resolved)}"
        assert resolved[0].perm_id == req.perm_id
        assert resolved[0].allowed is True
        wire = resolved[0].to_wire()
        assert wire["type"] == "permissionResolved"
        assert wire["permId"] == req.perm_id

    asyncio.run(run())


def test_permission_dismiss_emits_permission_resolved_denied():
    """A dismissal (deny_all via interrupt) emits PermissionResolved allowed=False,
    so reattached clients clear the card instead of resurrecting it."""
    from claude_agent_sdk import PermissionResultDeny
    from server.sessions.events import PermissionResolved

    async def run():
        d = SessionDriver("sp-resolved-dismiss", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=_factory([]), registry=PermissionRegistry(), identity="me")
        out_q: asyncio.Queue = asyncio.Queue()
        d._subscribers.add(out_q)
        callback_task = asyncio.create_task(d._can_use_tool("Bash", {"command": "ls"}))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        await d.interrupt()
        result = await asyncio.wait_for(callback_task, timeout=2.0)
        assert isinstance(result, PermissionResultDeny)

        resolved = []
        while not out_q.empty():
            e = out_q.get_nowait()
            if isinstance(e, PermissionResolved):
                resolved.append(e)
        assert len(resolved) == 1
        assert resolved[0].allowed is False

    asyncio.run(run())


def test_auto_allowed_permission_emits_no_permission_resolved():
    """An auto-approved tool (no PermissionRequest ever emitted) must not emit a
    PermissionResolved either — there is no card to clear."""
    from server.sessions.events import PermissionRequest, PermissionResolved

    async def run():
        d = SessionDriver("sp-auto", "primary", "/tmp/r", "/tmp/cfg", mode="auto",
                          sdk_factory=_factory([]), registry=PermissionRegistry(), identity="me")
        out_q: asyncio.Queue = asyncio.Queue()
        d._subscribers.add(out_q)
        await asyncio.wait_for(d._can_use_tool("Read", {"file_path": "/tmp/x"}), timeout=2.0)
        events = []
        while not out_q.empty():
            events.append(out_q.get_nowait())
        assert not any(isinstance(e, PermissionRequest) for e in events)
        assert not any(isinstance(e, PermissionResolved) for e in events)

    asyncio.run(run())


def test_askuserquestion_answer_emits_question_resolved():
    """Answering a question emits ev.QuestionResolved through the driver fan-out.

    The resolution must be an EVENT (not just client-local state): the daemon ring
    replays the original Question frame on a mid-turn reattach, so without a
    QuestionResolved frame in the same stream an already-answered question
    resurrects as pending in every reattached client (and other live tabs never
    learn it was answered at all)."""
    from server.sessions.events import Question, QuestionResolved

    async def run():
        d = SessionDriver("sq-resolved", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=_factory([]), registry=PermissionRegistry(), identity="me")
        out_q: asyncio.Queue = asyncio.Queue()
        d._subscribers.add(out_q)
        tool_input = {"questions": [{"header": "Choose", "question": "Favourite?",
                                     "options": [{"label": "Option A"}, {"label": "Option B"}],
                                     "multiSelect": False}]}
        callback_task = asyncio.create_task(d._can_use_tool("AskUserQuestion", tool_input))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        q_event = out_q.get_nowait()
        assert isinstance(q_event, Question)
        d.answer_question(q_event.question_id, ["Option A"], "me", mode="option")
        await asyncio.wait_for(callback_task, timeout=2.0)

        resolved = []
        while not out_q.empty():
            e = out_q.get_nowait()
            if isinstance(e, QuestionResolved):
                resolved.append(e)
        assert len(resolved) == 1, f"expected exactly one QuestionResolved, got {len(resolved)}"
        assert resolved[0].question_id == q_event.question_id
        assert resolved[0].turn_id == q_event.turn_id
        assert resolved[0].selected == ["Option A"]
        assert resolved[0].mode == "option"
        wire = resolved[0].to_wire()
        assert wire["type"] == "questionResolved"
        assert wire["questionId"] == q_event.question_id
        assert wire["selected"] == ["Option A"]

    asyncio.run(run())


def test_askuserquestion_dismiss_emits_question_resolved_dismissed():
    """A dismissal (deny_all via interrupt) emits QuestionResolved with selected=None,
    so reattached clients un-live the card instead of resurrecting it."""
    from server.sessions.events import QuestionResolved

    async def run():
        d = SessionDriver("sq-resolved-dismiss", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=_factory([]), registry=PermissionRegistry(), identity="me")
        out_q: asyncio.Queue = asyncio.Queue()
        d._subscribers.add(out_q)
        tool_input = {"questions": [{"header": "H", "question": "Q?",
                                     "options": [{"label": "Yes"}], "multiSelect": False}]}
        callback_task = asyncio.create_task(d._can_use_tool("AskUserQuestion", tool_input))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        await d.interrupt()
        await asyncio.wait_for(callback_task, timeout=2.0)

        resolved = []
        while not out_q.empty():
            e = out_q.get_nowait()
            if isinstance(e, QuestionResolved):
                resolved.append(e)
        assert len(resolved) == 1
        assert resolved[0].selected is None
        assert resolved[0].to_wire()["selected"] is None

    asyncio.run(run())


def test_askuserquestion_multi_question_each_answer_emits_resolved_immediately():
    """In a multi-question call, each answer emits its own QuestionResolved as soon
    as that future resolves — not batched after the whole gather — so a reattach
    between answers only resurrects the genuinely-unanswered cards."""
    from server.sessions.events import Question, QuestionResolved

    async def run():
        d = SessionDriver("sq-resolved-multi", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=_factory([]), registry=PermissionRegistry(), identity="me")
        out_q: asyncio.Queue = asyncio.Queue()
        d._subscribers.add(out_q)
        tool_input = {
            "questions": [
                {"header": "H1", "question": "Q1?", "options": [{"label": "Yes"}], "multiSelect": False},
                {"header": "H2", "question": "Q2?", "options": [{"label": "A"}], "multiSelect": False},
            ]
        }
        callback_task = asyncio.create_task(d._can_use_tool("AskUserQuestion", tool_input))
        for _ in range(4):
            await asyncio.sleep(0)
        q_events = []
        while not out_q.empty():
            e = out_q.get_nowait()
            if isinstance(e, Question):
                q_events.append(e)
        assert len(q_events) == 2

        # Answer ONLY the first question; its QuestionResolved must be emitted
        # before the second is answered.
        d.answer_question(q_events[0].question_id, ["Yes"], "me")
        for _ in range(4):
            await asyncio.sleep(0)
        first_resolved = []
        while not out_q.empty():
            e = out_q.get_nowait()
            if isinstance(e, QuestionResolved):
                first_resolved.append(e)
        assert len(first_resolved) == 1
        assert first_resolved[0].question_id == q_events[0].question_id

        d.answer_question(q_events[1].question_id, ["A"], "me")
        await asyncio.wait_for(callback_task, timeout=2.0)

    asyncio.run(run())


def test_askuserquestion_multi_question_single_call():
    """A tool_input with multiple questions emits one Question event per question
    and formats all answers in the deny message."""
    from claude_agent_sdk import PermissionResultDeny
    from server.sessions.events import Question

    async def run():
        d = SessionDriver("sq-multi", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=_factory([]), registry=PermissionRegistry(), identity="me")
        out_q: asyncio.Queue = asyncio.Queue()
        d._subscribers.add(out_q)
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
        while not out_q.empty():
            ev_item = out_q.get_nowait()
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
        out_q: asyncio.Queue = asyncio.Queue()
        d._subscribers.add(out_q)
        tool_input = {"questions": [{"question": "Pick one",
                                     "options": ["Alpha", "Beta"], "multiSelect": False}]}
        callback_task = asyncio.create_task(d._can_use_tool("AskUserQuestion", tool_input))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert not out_q.empty()
        q_event = out_q.get_nowait()
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

    class SlowFakeSDK(PerQueryScript):
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

    def factory(cwd, cfg, can_use_tool, mode, resume, *, on_pre_compact=None):
        return sdk

    async def run():
        d = SessionDriver("qu1", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=factory, registry=PermissionRegistry(), identity="me")
        out_q: asyncio.Queue = asyncio.Queue()
        d._subscribers.add(out_q)
        await d.start()
        await d.send_prompt("first")          # starts immediately (no QueueUpdate)
        await d.send_prompt("second")         # enqueued → QueueUpdate(["second"])
        await d.send_prompt("third")          # enqueued → QueueUpdate(["second","third"])

        # Drain what's in _out right now (non-blocking)
        events = []
        while not out_q.empty():
            events.append(out_q.get_nowait())

        await d.aclose()
        return events

    events = asyncio.run(run())
    queue_updates = [e for e in events if isinstance(e, QueueUpdate)]
    # Two enqueue operations → two QueueUpdate events
    assert len(queue_updates) == 2
    assert queue_updates[0].items == [{"text": "second", "images": []}]
    assert queue_updates[1].items == [{"text": "second", "images": []}, {"text": "third", "images": []}]


def test_cancel_queued_removes_item_and_emits_queue_update():
    """cancel_queued(0) removes the first queued item and emits a QueueUpdate + Status."""

    class SlowFakeSDK(PerQueryScript):
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return False
        async def query(self, prompt: str): pass
        async def receive_response(self):
            await asyncio.sleep(0.2)
            yield f.result(uuid="t1")
        async def interrupt(self): pass
        async def set_permission_mode(self, mode): pass

    def factory(cwd, cfg, can_use_tool, mode, resume, *, on_pre_compact=None):
        return SlowFakeSDK()

    async def run():
        d = SessionDriver("qc1", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=factory, registry=PermissionRegistry(), identity="me")
        out_q: asyncio.Queue = asyncio.Queue()
        d._subscribers.add(out_q)
        await d.start()
        await d.send_prompt("first")     # running
        await d.send_prompt("second")    # queued[0]
        await d.send_prompt("third")     # queued[1]
        assert d.queued() == ["second", "third"]

        # Drain enqueue events emitted so far
        while not out_q.empty():
            out_q.get_nowait()

        result = await d.cancel_queued(0)
        assert result is True
        assert d.queued() == ["third"]

        # The QueueUpdate and Status should have been emitted
        events = []
        while not out_q.empty():
            events.append(out_q.get_nowait())

        queue_updates = [e for e in events if isinstance(e, QueueUpdate)]
        statuses = [e for e in events if isinstance(e, Status)]
        assert len(queue_updates) == 1
        assert queue_updates[0].items == [{"text": "third", "images": []}]
        assert any(s.count == 1 for s in statuses)

        await d.aclose()

    asyncio.run(run())


def test_cancel_queued_out_of_range_returns_false():
    """cancel_queued with an out-of-range index returns False without emitting events."""

    class SlowFakeSDK(PerQueryScript):
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return False
        async def query(self, prompt: str): pass
        async def receive_response(self):
            await asyncio.sleep(0.2)
            yield f.result(uuid="t1")
        async def interrupt(self): pass
        async def set_permission_mode(self, mode): pass

    def factory(cwd, cfg, can_use_tool, mode, resume, *, on_pre_compact=None):
        return SlowFakeSDK()

    async def run():
        d = SessionDriver("qc2", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=factory, registry=PermissionRegistry(), identity="me")
        out_q: asyncio.Queue = asyncio.Queue()
        d._subscribers.add(out_q)
        await d.start()
        await d.send_prompt("first")
        await d.send_prompt("second")
        # Drain setup events
        while not out_q.empty():
            out_q.get_nowait()

        result_neg = await d.cancel_queued(-1)
        result_oob = await d.cancel_queued(5)
        assert result_neg is False
        assert result_oob is False
        # No events emitted for invalid indices
        assert out_q.empty()

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
            if isinstance(e, Status) and e.state in ("idle", "done", "complete", "needs_attention"):
                break
        if d._pump:
            await d._pump
        await d.aclose()
    asyncio.run(run())
    assert "in_progress" in seen, f"expected an 'in_progress' status, got {seen}"
    assert seen and seen[-1] == "done", f"expected to end 'done', got {seen}"


def test_askuserquestion_freeform_framing():
    """A freeform answer is framed as the user's own words."""
    from claude_agent_sdk import PermissionResultDeny
    from server.sessions.events import Question

    async def run():
        d = SessionDriver("sq-ff", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=_factory([]), registry=PermissionRegistry(), identity="me")
        out_q: asyncio.Queue = asyncio.Queue()
        d._subscribers.add(out_q)
        tool_input = {"questions": [{"header": "Choose", "question": "Favourite?",
                                     "options": [{"label": "Option A"}], "multiSelect": False}]}
        callback_task = asyncio.create_task(d._can_use_tool("AskUserQuestion", tool_input))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        q_event = out_q.get_nowait()
        assert isinstance(q_event, Question)

        d.answer_question(q_event.question_id, ["I prefer C, actually"], "me", mode="freeform")
        result = await asyncio.wait_for(callback_task, timeout=2.0)
        assert isinstance(result, PermissionResultDeny)
        assert "[User answered in their own words]" in result.message
        assert "I prefer C, actually" in result.message

    asyncio.run(run())


def test_askuserquestion_chat_framing():
    """A chat answer is framed as a discussion redirect, not a rigid selection."""
    from claude_agent_sdk import PermissionResultDeny
    from server.sessions.events import Question

    async def run():
        d = SessionDriver("sq-chat", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=_factory([]), registry=PermissionRegistry(), identity="me")
        out_q: asyncio.Queue = asyncio.Queue()
        d._subscribers.add(out_q)
        tool_input = {"questions": [{"header": "Choose", "question": "Favourite?",
                                     "options": [{"label": "Option A"}], "multiSelect": False}]}
        callback_task = asyncio.create_task(d._can_use_tool("AskUserQuestion", tool_input))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        q_event = out_q.get_nowait()
        assert isinstance(q_event, Question)

        d.answer_question(q_event.question_id, ["why are you asking this?"], "me", mode="chat")
        result = await asyncio.wait_for(callback_task, timeout=2.0)
        assert isinstance(result, PermissionResultDeny)
        assert "[User wants to discuss rather than pick an option]" in result.message
        assert "why are you asking this?" in result.message

    asyncio.run(run())


def test_queue_wire_includes_images_for_queued_prompt():
    """A prompt queued while busy is exposed via queue_wire() with its image refs."""
    import asyncio

    class SlowFakeSDK(PerQueryScript):
        def __init__(self): self.queries = []
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return False
        async def query(self, prompt): self.queries.append(prompt)
        async def receive_response(self):
            await asyncio.sleep(0.05)
            yield f.result(uuid="t1")
        async def interrupt(self): pass
        async def set_permission_mode(self, mode): pass

    sdk = SlowFakeSDK()
    def factory(cwd, cfg, can_use_tool, mode, resume, *, on_pre_compact=None): return sdk
    images = [{"sha": "a", "mime": "image/png", "name": "s.png",
               "path": "/tmp/x", "url": "/api/attachments/a"}]

    async def run():
        d = SessionDriver("sq", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=factory, registry=PermissionRegistry(), identity="me")
        await d.start()
        await d.send_prompt("first")            # starts the (slow) turn
        await d.send_prompt("second", images)   # queues behind it
        wire = d.queue_wire()
        await d.aclose()
        return wire

    wire = asyncio.run(run())
    assert wire == [{"text": "second",
                     "images": [{"url": "/api/attachments/a", "mime": "image/png", "name": "s.png"}]}]


# ── Recovering / emit_error ──────────────────────────────────────────────────

def _drain_queue(q):
    """Drain an asyncio.Queue and return all items as a list."""
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def test_dropped_turn_emits_recovering_not_error():
    """A turn_dropped ErrorEvent should become a Recovering event, not a red error."""
    # Build a ResultMessage that the translator renders as
    # ErrorEvent(code="execution_error", raw_message="[ede_diagnostic] ... stop_reason=null")
    msgs = [
        f.stream_start(uuid="t1"),
        f.result(uuid="t1", subtype="error_during_execution", is_error=True,
                 errors=["[ede_diagnostic] result_type=user last_content_type=n/a "
                         "stop_reason=null"]),
    ]

    async def run():
        d = SessionDriver("rec1", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=_factory(msgs), registry=PermissionRegistry(), identity="me")
        await d.start()
        await d.send_prompt("go")
        # Collect until the turn ends (drain completes and idle status is emitted).
        out = []
        async for e in d.events():
            out.append(e)
            if isinstance(e, Status) and e.state in ("idle", "done", "complete", "needs_attention"):
                break
        await d.aclose()
        return out

    out = asyncio.run(run())
    kinds = [type(e).__name__ for e in out]
    assert "Recovering" in kinds
    assert "ErrorEvent" not in kinds  # red error suppressed for dropped turn


def test_emit_error_puts_errorevent():
    """emit_error() anchors ErrorEvent to _last_turn_id (the dropped turn's real id),
    falling back to self._turn() when _last_turn_id is empty."""
    msgs = [f.result(uuid="t1")]  # minimal: just enough for the driver to init

    async def run_with_last_turn_id():
        d = SessionDriver("ee1", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=_factory(msgs), registry=PermissionRegistry(), identity="me")
        out_q: asyncio.Queue = asyncio.Queue()
        d._subscribers.add(out_q)
        await d.start()
        d._last_turn_id = "t7"  # simulate post-drain state where translator already reset
        await d.emit_error("turn_dropped_cap", "gave up")
        return _drain_queue(out_q)

    out = asyncio.run(run_with_last_turn_id())
    err = next(e for e in out if isinstance(e, ev.ErrorEvent))
    assert err.code == "turn_dropped_cap"
    assert err.turn_id == "t7", f"expected turn_id='t7', got {err.turn_id!r}"

    async def run_without_last_turn_id():
        d = SessionDriver("ee2", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=_factory(msgs), registry=PermissionRegistry(), identity="me")
        out_q: asyncio.Queue = asyncio.Queue()
        d._subscribers.add(out_q)
        await d.start()
        # _last_turn_id is "" (empty) — falls back to self._turn()
        await d.emit_error("turn_dropped_cap", "gave up")
        return _drain_queue(out_q)

    out2 = asyncio.run(run_without_last_turn_id())
    err2 = next(e for e in out2 if isinstance(e, ev.ErrorEvent))
    assert err2.code == "turn_dropped_cap"
    assert err2.turn_id == "turn"  # _turn() fallback when _last_turn_id is empty
