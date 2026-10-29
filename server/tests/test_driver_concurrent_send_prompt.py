"""Two ``send_prompt`` calls that land together on an idle driver must start ONE turn.

Unarchiving an auto-archived session used to stage a "why you're back" seed that the WS bridge
delivers on the first live attach (``pop_pending_initial_prompt`` → ``send_prompt``). A
human who re-prompts at the same moment sends a second ``send_prompt`` milliseconds
later. ``send_prompt`` reads ``_turn_live()`` before calling ``_start_turn``, and
``_start_turn`` only sets ``_turn_active`` after ``await self._stop_listener()`` — so
both calls saw an idle driver, both queried the SDK, and both spawned a pump over the
one shared ``_MessageSource``. The pumps then drove one ``receive_messages()`` generator
concurrently::

    RuntimeError: anext(): asynchronous generator is already running

(rocky daemon, 2026-09-24 09:44:24 — the session then recomposed on ``process_crash``).
"""
import asyncio

from server.sessions import events as ev
from server.sessions.claude_driver import ClaudeAgentDriver
from server.sessions.permissions import PermissionRegistry
from server.tests.fakes.fake_sdk import FakeSDKClient


def _driver(client: FakeSDKClient) -> ClaudeAgentDriver:
    return ClaudeAgentDriver("s-concurrent", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                             sdk_factory=lambda *a, **k: client, registry=PermissionRegistry(),
                             identity="me")


async def _wait_until(pred, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.005)


def test_concurrent_send_prompts_start_one_turn_and_queue_the_other():
    client = FakeSDKClient([])

    async def run():
        d = _driver(client)
        await d.start()
        seen: list = []

        async def collect():
            async for e in d.events():
                seen.append(e)
        collector = asyncio.create_task(collect())
        await asyncio.sleep(0.01)

        await asyncio.gather(d.send_prompt("seed"), d.send_prompt("human"))
        await _wait_until(lambda: not d.turn_active and not d._queue)

        # Both prompts reach the CLI, in FIFO order, one turn at a time — never two
        # pumps racing the one shared cursor. The buggy code let both calls slip past
        # `_turn_live()` before either claimed the turn: both queried the SDK directly
        # (client.sent came back re-ordered, ['human', 'seed']) and each spawned its
        # own pump over the one _MessageSource, producing a stray extra TurnEnd where
        # the real driver instead raises "anext(): asynchronous generator is already
        # running" against the CLI's actual receive_messages() stream.
        assert client.sent == ["seed", "human"], client.sent
        user_msgs = [e for e in seen if isinstance(e, ev.UserMessage)]
        turn_ends = [e for e in seen if isinstance(e, ev.TurnEnd)]
        assert len(user_msgs) == 2, user_msgs
        assert len(turn_ends) == 2, turn_ends
        errors = [e for e in seen if isinstance(e, ev.ErrorEvent)]
        assert errors == [], errors
        assert d._stream_ended is False
        await d.aclose()
        collector.cancel()

    asyncio.run(run())
