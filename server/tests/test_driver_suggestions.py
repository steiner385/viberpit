import asyncio
from server.sessions.driver import SessionDriver
from server.sessions.permissions import PermissionRegistry
from server.sessions.events import Suggestions
from server.tests.fakes.fake_sdk import FakeSDKClient
from server.tests.fakes import fake_sdk as f


def _factory(msgs):
    def make(cwd, config_dir, can_use_tool, mode, resume, extra_env=None, *, on_pre_compact=None):
        return FakeSDKClient(msgs, on_can_use_tool=can_use_tool)
    return make


class _StubSuggester:
    def __init__(self, items, gate: asyncio.Event | None = None):
        self.items = items
        self.gate = gate
        self.calls = 0

    async def generate(self, cwd, config_dir, context, account=None):
        self.calls += 1
        if self.gate is not None:
            await self.gate.wait()
        return self.items


def test_emits_suggestions_after_idle():
    msgs = [f.stream_start(uuid="t1"), f.stream_text("done", uuid="t1"), f.result(uuid="t1")]
    sug = _StubSuggester(["Run the tests", "Commit it"])

    async def run():
        d = SessionDriver("s1", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=_factory(msgs), registry=PermissionRegistry(),
                          identity="me", suggester=sug)
        await d.start()
        _viewer = d.open_stream()   # attached viewer: suggestions are gated on one
        await d.send_prompt("hi")
        got = []
        async def consume():
            async for e in d.events():
                got.append(e)
                if isinstance(e, Suggestions):
                    return
        await asyncio.wait_for(asyncio.create_task(consume()), timeout=1.0)
        await d.aclose()
        return got
    got = asyncio.run(run())
    sug_ev = next(e for e in got if isinstance(e, Suggestions))
    assert sug_ev.items == ["Run the tests", "Commit it"]
    assert sug_ev.session_id == "s1"


def test_empty_suggestions_emit_no_event():
    msgs = [f.stream_start(uuid="t1"), f.stream_text("done", uuid="t1"), f.result(uuid="t1")]
    sug = _StubSuggester([])

    async def run():
        d = SessionDriver("s1", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=_factory(msgs), registry=PermissionRegistry(),
                          identity="me", suggester=sug)
        await d.start()
        _viewer = d.open_stream()   # attached viewer: suggestions are gated on one
        await d.send_prompt("hi")
        got = []
        async def consume():
            async for e in d.events():
                got.append(e)
        t = asyncio.create_task(consume())
        await asyncio.sleep(0.1)
        if d._suggest_task:
            await d._suggest_task
        await asyncio.sleep(0.05)
        await d.aclose()
        await t
        return got
    got = asyncio.run(run())
    assert sug.calls == 1
    assert not any(isinstance(e, Suggestions) for e in got)


def test_no_suggester_never_triggers():
    msgs = [f.stream_start(uuid="t1"), f.stream_text("done", uuid="t1"), f.result(uuid="t1")]

    async def run():
        d = SessionDriver("s1", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=_factory(msgs), registry=PermissionRegistry(),
                          identity="me")
        await d.start()
        await d.send_prompt("hi")
        got = []
        async def consume():
            async for e in d.events():
                got.append(e)
        t = asyncio.create_task(consume())
        await asyncio.sleep(0.1)
        await d.aclose()
        await t
        return d._suggest_task, got
    suggest_task, got = asyncio.run(run())
    assert suggest_task is None
    assert not any(isinstance(e, Suggestions) for e in got)


def test_new_prompt_cancels_inflight_suggestion():
    msgs = [f.stream_start(uuid="t1"), f.stream_text("done", uuid="t1"), f.result(uuid="t1")]
    gate = asyncio.Event()  # never set → generate() blocks until cancelled
    sug = _StubSuggester(["late"], gate=gate)

    async def run():
        d = SessionDriver("s1", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=_factory(msgs), registry=PermissionRegistry(),
                          identity="me", suggester=sug)
        await d.start()
        got = []
        async def consume():
            async for e in d.events():
                got.append(e)
        t = asyncio.create_task(consume())
        await d.send_prompt("hi")
        await asyncio.sleep(0.1)            # idle → suggestion task launched, blocked on gate
        inflight = d._suggest_task
        await d.send_prompt("second")       # must cancel the in-flight suggestion task
        await asyncio.sleep(0.05)
        cancelled = inflight.cancelled()
        await d.aclose()
        await t
        return cancelled, got
    cancelled, got = asyncio.run(run())
    assert cancelled is True
    assert not any(isinstance(e, Suggestions) for e in got)


def test_seed_idle_suggestions_emits_once(caplog):
    sug = _StubSuggester(["Resume hint"])

    async def run():
        d = SessionDriver("s1", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=_factory([]), registry=PermissionRegistry(),
                          identity="me", suggester=sug)
        await d.start()
        _viewer = d.open_stream()   # attached viewer: suggestions are gated on one
        d.seed_idle_suggestions("do the thing", "earlier answer")
        got = []
        async def consume():
            async for e in d.events():
                got.append(e)
                if isinstance(e, Suggestions):
                    return
        await asyncio.wait_for(asyncio.create_task(consume()), timeout=1.0)
        d.seed_idle_suggestions("do the thing", "earlier answer")  # 2nd attach: no-op
        await asyncio.sleep(0.05)
        await d.aclose()
        return got
    with caplog.at_level("INFO"):
        got = asyncio.run(run())
    sug_ev = next(e for e in got if isinstance(e, Suggestions))
    assert sug_ev.items == ["Resume hint"]
    assert sug.calls == 1
    assert "suggestions session=s1" in caplog.text


def test_seed_idle_suggestions_noop_without_context():
    sug = _StubSuggester(["x"])

    async def run():
        d = SessionDriver("s1", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=_factory([]), registry=PermissionRegistry(),
                          identity="me", suggester=sug)
        await d.start()
        d.seed_idle_suggestions("   ", "")  # blank → no generation
        await asyncio.sleep(0.05)
        await d.aclose()
        return d._suggest_task
    task = asyncio.run(run())
    assert task is None
    assert sug.calls == 0


def test_no_subscribers_skips_suggestions():
    """Headless/automation sessions (no attached viewer) must not spend tokens
    generating suggestions no human will read — the subscriber gate skips them."""
    msgs = [f.stream_start(uuid="t1"), f.stream_text("done", uuid="t1"), f.result(uuid="t1")]
    sug = _StubSuggester(["never generated"])

    async def run():
        d = SessionDriver("s1", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=_factory(msgs), registry=PermissionRegistry(),
                          identity="me", suggester=sug)
        await d.start()
        await d.send_prompt("hi")   # no open_stream() → no subscriber
        if d._pump is not None:
            await d._pump            # let the turn settle (drain fires the gate)
        await asyncio.sleep(0.05)
        task = d._suggest_task
        await d.aclose()
        return task
    task = asyncio.run(run())
    assert task is None
    assert sug.calls == 0


def test_copilot_account_skips_suggestions(monkeypatch):
    """Copilot sessions run a third-party model — Haiku suggestions routed through
    the proxy are meaningless (mirrors the detached-path guard at orchestrator:4947)."""
    monkeypatch.setattr("server.sessions.driver._provider_of",
                        lambda name: "copilot")
    msgs = [f.stream_start(uuid="t1"), f.stream_text("done", uuid="t1"), f.result(uuid="t1")]
    sug = _StubSuggester(["never generated"])

    async def run():
        d = SessionDriver("s1", "copilot-acct", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=_factory(msgs), registry=PermissionRegistry(),
                          identity="me", suggester=sug)
        await d.start()
        _viewer = d.open_stream()   # subscriber present → isolates the copilot gate
        await d.send_prompt("hi")
        if d._pump is not None:
            await d._pump
        await asyncio.sleep(0.05)
        task = d._suggest_task
        await d.aclose()
        return task
    task = asyncio.run(run())
    assert task is None
    assert sug.calls == 0


def test_identical_context_dedupes_generation():
    """Two turn-boundaries with byte-identical context must generate suggestions
    once — the signature cache short-circuits the redundant LLM call."""
    sug = _StubSuggester(["Run the tests"])

    async def run():
        d = SessionDriver("s1", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=_factory([]), registry=PermissionRegistry(),
                          identity="me", suggester=sug)
        await d.start()
        _viewer = d.open_stream()
        await d._run_suggestions("User: hi\n\nAssistant: done", "t1")
        await d._run_suggestions("User: hi\n\nAssistant: done", "t2")  # identical → deduped
        after_dup = sug.calls
        await d._run_suggestions("User: hi\n\nAssistant: CHANGED", "t3")  # new → regenerates
        after_new = sug.calls
        await d.aclose()
        return after_dup, after_new
    after_dup, after_new = asyncio.run(run())
    assert after_dup == 1
    assert after_new == 2


def test_orchestrator_disabled_by_env(monkeypatch):
    monkeypatch.setenv("CCSWITCH_SUGGESTIONS", "0")
    from server.sessions.orchestrator import SessionOrchestrator
    orch = SessionOrchestrator(sdk_factory=_factory([]))
    assert orch._suggester is None


def test_orchestrator_enabled_by_default(monkeypatch):
    monkeypatch.delenv("CCSWITCH_SUGGESTIONS", raising=False)
    from server.sessions.orchestrator import SessionOrchestrator
    orch = SessionOrchestrator(sdk_factory=_factory([]))
    assert orch._suggester is not None
