"""The CLI can run a turn ON ITS OWN — a background task's ``<task-notification>`` is
injected by the CLI as a user message and the model continues — with no SDK ``query()``
behind it. The driver must ALWAYS be draining the SDK stream: with nobody reading, those
messages sit in the SDK's bounded channel (cap 100, then the CLI blocks on stdout) and
are replayed into the NEXT prompt's turn, whose stale ResultMessage closes that turn at
once with the WRONG outcome.

Incident 2026-09-03 (session 2a158cb3): a parked session's autonomous continuation
ended with the no-PR marker while idle; the user's next prompt "yes, please open the
PRs" was answered by the stale buffered turn → ``complete`` → the completion release
SIGTERMed the CLI while it was actually answering the real prompt → silence.
"""
import asyncio

from claude_agent_sdk import SystemMessage, TextBlock

from server.sessions.driver import SessionDriver, _MessageSource, _EOF
from server.sessions.events import TurnEnd, Status, QueueUpdate
from server.sessions.permissions import PermissionRegistry
from server.tests.fakes import fake_sdk as f
from server.tests.fakes.fake_sdk import FakeSDKClient

MARKER = "<!-- ccswitch:no-pr -->"


def _autonomous_turn(uuid_prefix: str = "a"):
    """A CLI-initiated continuation: tool call, tool result, final text carrying the
    no-PR marker, and the CLI's own ResultMessage."""
    return [
        f.tool_use("toolu_bg", "Bash", {"command": "cat /tmp/timing.log"}, uuid=f"{uuid_prefix}1"),
        f.user_tool_result("toolu_bg", "ALL DONE", uuid=f"{uuid_prefix}2"),
        f.assistant(TextBlock(text=f"Here's the answer.\n\n{MARKER}"), uuid=f"{uuid_prefix}3"),
        f.result(uuid=f"{uuid_prefix}9"),
    ]


async def _wait_until(pred, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.005)


def _build(client, statuses: list[str] | None = None) -> SessionDriver:
    async def on_status(sid, status):
        if statuses is not None:
            statuses.append(status)
    return SessionDriver("s-auto", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                         sdk_factory=lambda *a, **k: client, registry=PermissionRegistry(),
                         identity="me", on_status=on_status)


def _collector(d: SessionDriver):
    seen: list = []

    async def collect():
        async for e in d.events():
            seen.append(e)
    task = asyncio.create_task(collect())
    return seen, task


def test_autonomous_turn_is_drained_while_idle():
    """No prompt in flight: messages the CLI emits on its own must still be consumed,
    tracked as a turn (status in_progress → complete) and closed on their own result."""
    client = FakeSDKClient([])
    statuses: list[str] = []

    async def run():
        d = _build(client, statuses)
        await d.start()
        seen, task = _collector(d)
        await asyncio.sleep(0.01)
        assert d.turn_active is False
        client.inject(_autonomous_turn())
        await _wait_until(lambda: any(isinstance(e, TurnEnd) for e in seen))
        await _wait_until(lambda: not d.turn_active)
        await d.aclose()
        task.cancel()
        return seen, d
    seen, d = asyncio.run(run())
    end = next(e for e in seen if isinstance(e, TurnEnd))
    assert end.outcome == "complete"
    assert end.pr_resolution == "not-needed"
    states = [e.state for e in seen if isinstance(e, Status)]
    assert "in_progress" in states, states
    assert states.index("in_progress") < len(states) - 1
    assert statuses[-1] == "complete", statuses
    assert client.sent == []          # nothing was queried — the CLI did this itself


def test_prompt_after_autonomous_turn_sees_only_its_own_result():
    """The regression: the stale autonomous result must never be attributed to the
    next prompt. The prompt's own turn ends on ITS result with ITS outcome."""
    client = FakeSDKClient([f.assistant(TextBlock(text="opening PRs"), uuid="p1"),
                            f.result(uuid="p9")])

    async def run():
        d = _build(client)
        await d.start()
        seen, task = _collector(d)
        client.inject(_autonomous_turn())
        await _wait_until(lambda: any(isinstance(e, TurnEnd) for e in seen))
        await _wait_until(lambda: not d.turn_active)
        await d.send_prompt("yes, please open the PRs")
        await _wait_until(lambda: sum(isinstance(e, TurnEnd) for e in seen) == 2)
        await _wait_until(lambda: not d.turn_active)
        await d.aclose()
        task.cancel()
        return seen
    seen = asyncio.run(run())
    ends = [e for e in seen if isinstance(e, TurnEnd)]
    assert [e.outcome for e in ends] == ["complete", "success"]
    assert ends[0].turn_id != ends[1].turn_id
    assert client.sent == ["yes, please open the PRs"]


def test_autonomous_turn_after_a_prompt_turn_is_still_drained():
    """The incident ordering: a prompt turn ends (the CLI parks on a background task),
    then the CLI continues on its own. The listener must be back on the stream after
    the prompt's pump settles — not only after start()."""
    client = FakeSDKClient([f.assistant(TextBlock(text="I'll wait for the task."), uuid="p1"),
                            f.result(uuid="p9")])
    statuses: list[str] = []

    async def run():
        d = _build(client, statuses)
        await d.start()
        seen, task = _collector(d)
        await d.send_prompt("how much does the type-check cost?")
        await _wait_until(lambda: any(isinstance(e, TurnEnd) for e in seen))
        await _wait_until(lambda: not d.turn_active)
        client.inject(_autonomous_turn())
        await _wait_until(lambda: sum(isinstance(e, TurnEnd) for e in seen) == 2)
        await _wait_until(lambda: not d.turn_active)
        await d.aclose()
        task.cancel()
        return seen
    seen = asyncio.run(run())
    ends = [e for e in seen if isinstance(e, TurnEnd)]
    assert [e.outcome for e in ends] == ["success", "complete"]
    assert statuses[-1] == "complete", statuses


def test_prompt_during_autonomous_turn_is_queued_until_it_ends():
    """A prompt typed while the CLI is mid-continuation joins the queue (like a prompt
    during any live turn) and is dispatched once the continuation's result lands."""
    client = FakeSDKClient([f.assistant(TextBlock(text="ok"), uuid="p1"), f.result(uuid="p9")])
    head = _autonomous_turn()[:-1]   # everything but the result: the turn stays open

    async def run():
        d = _build(client)
        await d.start()
        seen, task = _collector(d)
        client.inject(head)
        await _wait_until(lambda: d.turn_active)
        await d.send_prompt("go")
        await asyncio.sleep(0.02)
        queued_while_open = list(d.queued())
        sent_while_open = list(client.sent)
        client.inject([f.result(uuid="a9")])
        await _wait_until(lambda: client.sent == ["go"])
        await _wait_until(lambda: sum(isinstance(e, TurnEnd) for e in seen) == 2)
        await _wait_until(lambda: not d.turn_active)
        await d.aclose()
        task.cancel()
        return seen, queued_while_open, sent_while_open
    seen, queued_while_open, sent_while_open = asyncio.run(run())
    assert queued_while_open == ["go"]
    assert sent_while_open == []
    assert any(isinstance(e, QueueUpdate) and e.items and e.items[0]["text"] == "go"
               for e in seen)
    assert client.sent == ["go"]


def test_message_source_does_not_lose_a_message_on_cancel():
    """The hand-off between the idle listener and a prompt's drain cancels the waiter.
    anyio's memory-stream receive() can drop an item delivered in the same tick as the
    cancel, so the cursor must keep the in-flight ``__anext__`` alive and hand its
    result to the next caller."""
    async def run():
        gate = asyncio.Event()

        async def gen():
            await gate.wait()
            yield "m1"
            yield "m2"

        class _C:
            def receive_messages(self):
                return gen()

        src = _MessageSource(_C())
        waiter = asyncio.create_task(src.next())
        await asyncio.sleep(0)            # waiter is parked inside __anext__
        gate.set()                        # item becomes deliverable …
        waiter.cancel()                   # … and the waiter is cancelled in the same tick
        try:
            await waiter
        except asyncio.CancelledError:
            pass
        first = await src.next()
        second = await src.next()
        third = await src.next()
        return first, second, third
    assert asyncio.run(run()) == ("m1", "m2", _EOF)


def test_aclose_while_idle_listening_returns_promptly():
    """An idle driver holds a listener parked on the SDK stream; closing must not wait
    out the close grace for it (the grace is for a live turn's flush)."""
    client = FakeSDKClient([])

    async def run():
        d = _build(client)
        d._close_grace = 1.5
        await d.start()
        await asyncio.sleep(0.01)
        t0 = asyncio.get_running_loop().time()
        await d.aclose()
        return asyncio.get_running_loop().time() - t0
    assert asyncio.run(run()) < 1.0


def test_hook_frames_while_idle_do_not_open_a_turn():
    """SessionStart hooks make the CLI emit hook lifecycle frames right after connect,
    before any prompt (the fake delivers ``f.session_start_hook_frames()`` on connect,
    as the real CLI does). They are bookkeeping, not a turn: no ResultMessage will ever
    follow them. The listener must NOT open a phantom turn on them — if it does, the
    deferred initial prompt queues behind a turn that never closes (incident 2026-09-04:
    three fresh sessions on rocky stuck at "Working… · 1 queued", "No messages yet")."""
    client = FakeSDKClient([f.assistant(TextBlock(text="hi back"), uuid="p1"), f.result(uuid="p9")])
    statuses: list[str] = []

    async def run():
        d = _build(client, statuses)
        await d.start()
        seen, task = _collector(d)
        await asyncio.sleep(0.02)
        active_after_hooks = d.turn_active
        await d.send_prompt("hello")
        await asyncio.sleep(0.02)
        queued_after_send = list(d.queued())
        await _wait_until(lambda: any(isinstance(e, TurnEnd) for e in seen))
        await _wait_until(lambda: not d.turn_active)
        await d.aclose()
        task.cancel()
        return active_after_hooks, queued_after_send
    active_after_hooks, queued_after_send = asyncio.run(run())
    assert active_after_hooks is False
    assert queued_after_send == []
    assert client.sent == ["hello"]


def test_system_bookkeeping_frames_while_idle_do_not_open_a_turn():
    """The same holds for every SystemMessage the CLI emits between turns (task
    lifecycle notices, hook progress): a CLI-initiated turn announces itself with the
    user message the CLI injects, and THAT opens the turn — not the notice before it."""
    client = FakeSDKClient([])

    async def run():
        d = _build(client)
        await d.start()
        seen, task = _collector(d)
        client.inject([SystemMessage(subtype="task_notification", data={"task_id": "t1"})])
        await asyncio.sleep(0.02)
        idle_after_notice = not d.turn_active
        client.inject(_autonomous_turn("n"))
        await _wait_until(lambda: any(isinstance(e, TurnEnd) for e in seen))
        await _wait_until(lambda: not d.turn_active)
        await d.aclose()
        task.cancel()
        return idle_after_notice
    assert asyncio.run(run()) is True
