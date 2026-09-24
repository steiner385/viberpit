"""A prompt that lands on an already-disconnected SDK client is a crash, not a 500.

Incident (#1997, dobby, 2026-09-08): four ``ws.work failed … op=prompt`` errors in
~2.5 hours, each an opaque ``DaemonError: CLIConnectionError: Not connected. Call
connect() first.`` surfaced at the edge. ``send_prompt``'s dead-stream guard reads
``_stream_ended``, which is set *asynchronously* — only once a reader coroutine
(``_drain`` / the idle listener) is scheduled and observes the dead cursor. Between the
transport actually dying and that observation, every guard says "live", so the prompt
reaches ``_start_turn`` and calls ``self._client.query(...)`` on a dead client. The SDK
raised, the exception escaped the daemon's RPC handler as an opaque error, and the
driver was left mid-turn: ``_turn_active`` true, no pump, no recovery — the session sat
on "Working" until the stall watchdog swept it.

This is the same anti-pattern PR #517 fixed for ``interrupt()`` (existence-checked
``if self._client:`` instead of connection-state-checked). ``_start_turn``'s ``query``
never got the equivalent guard.

The contract pinned here: a ``CLIConnectionError`` from the turn-opening ``query`` is
treated exactly like the reader noticing the same death one scheduling tick later —
mark the stream dead, settle the turn, signal ONE ``process_crash`` so the orchestrator
recomposes the driver and replays the prompt. The caller sees no exception.
"""
import asyncio
import logging

from claude_agent_sdk._errors import CLIConnectionError

from server.sessions.claude_driver import ClaudeAgentDriver
from server.sessions.events import ErrorEvent, TurnFailure
from server.sessions.permissions import PermissionRegistry
from server.tests.fakes.fake_sdk import FakeSDKClient

_DRIVER_LOGGER = "server.sessions.claude_driver"


class _DisconnectedOnQueryClient(FakeSDKClient):
    """The SDK client after its transport died: ``query`` raises the SDK's own
    "Not connected" error, while ``receive_messages`` has not yet been scheduled to
    notice — the exact race window the incident lands in. ``fail_after`` lets a turn
    run normally first, so the queued-prompt dequeue path can be driven too."""

    def __init__(self, messages, *, fail_after: int = 0, **kw):
        super().__init__(messages, **kw)
        self._fail_after = fail_after
        self.queries = 0

    async def query(self, prompt):
        self.queries += 1
        if self.queries > self._fail_after:
            raise CLIConnectionError("Not connected. Call connect() first.")
        await super().query(prompt)


async def _wait_until(pred, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.005)


def _build(client, failures: list) -> ClaudeAgentDriver:
    async def on_fail(sid, kind, retry_after):
        failures.append((sid, kind, retry_after))
    return ClaudeAgentDriver("s-dc", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                             sdk_factory=lambda *a, **k: client, registry=PermissionRegistry(),
                             identity="me", on_recoverable_failure=on_fail)


def _collector(d: ClaudeAgentDriver):
    seen: list = []

    async def collect():
        async for e in d.events():
            seen.append(e)
    return seen, asyncio.create_task(collect())


def test_prompt_on_disconnected_client_recovers_instead_of_raising():
    client = _DisconnectedOnQueryClient([], fail_after=0)
    failures: list = []

    async def run():
        d = _build(client, failures)
        await d.start()
        seen, task = _collector(d)
        # Before the fix this propagated CLIConnectionError out of send_prompt, up
        # through the daemon RPC, and reached the edge as an opaque DaemonError.
        await d.send_prompt("go")
        await _wait_until(lambda: failures)
        await _wait_until(lambda: any(isinstance(e, TurnFailure) for e in seen))

        assert failures == [("s-dc", "process_crash", None)]
        assert d.stream_ended is True
        # The turn must settle: no pump was ever started, so nothing else will close it.
        assert d.turn_active is False
        assert [e.kind for e in seen if isinstance(e, TurnFailure)] == ["process_crash"]
        # Recovery owns the surface — no red error card for a prompt that will be replayed.
        assert [e for e in seen if isinstance(e, ErrorEvent)] == []
        # A later prompt is held for the rebuilt driver, not written into the void.
        await d.send_prompt("again")
        assert d.queued() == ["again"]
        assert client.queries == 1
        await d.aclose()
        task.cancel()
    asyncio.run(run())


def test_disconnected_query_is_reported_once(caplog):
    """One stream death, one log record — ``_on_stream_ended``'s ERROR.

    The error-triage pipeline fingerprints the normalized message and files an issue
    per WARNING-or-worse log site, so a line of the guard's own would file a second
    auto-error for the same incident (the shape behind #1943, and behind #1783/#1784).
    The mid-turn sibling in ``_drain`` already logs nothing of its own; pinned here so
    a future second line fails instead of quietly re-filing.
    """
    client = _DisconnectedOnQueryClient([], fail_after=0)
    failures: list = []

    async def run():
        d = _build(client, failures)
        await d.start()
        await d.send_prompt("go")
        await _wait_until(lambda: failures)
        await d.aclose()

    with caplog.at_level(logging.DEBUG, logger=_DRIVER_LOGGER):
        asyncio.run(run())

    deaths = [r for r in caplog.records
              if r.name == _DRIVER_LOGGER and r.levelno >= logging.WARNING]
    assert len(deaths) == 1, [r.getMessage() for r in deaths]
    assert deaths[0].getMessage().startswith("driver.stream_ended:")
    assert "mid_turn=True" in deaths[0].getMessage()


def test_dequeued_prompt_on_disconnected_client_settles_the_turn():
    """The same death one turn later: the client dies while turn one runs, and the
    prompt queued behind it is dequeued onto the corpse by ``_end_turn``. That call
    sits in the pump's ``finally``, so an escaping exception is an unretrieved task
    error and the session stays "Working" forever."""
    client = _DisconnectedOnQueryClient([], fail_after=1)
    failures: list = []

    async def run():
        d = _build(client, failures)
        await d.start()
        seen, task = _collector(d)
        await d.send_prompt("go")          # turn one: queried while still connected
        await d.send_prompt("early")       # queued behind the live turn
        assert d.queued() == ["early"]
        await _wait_until(lambda: failures)
        await _wait_until(lambda: not d.drain_alive, timeout=1.0)

        assert failures == [("s-dc", "process_crash", None)]
        assert d.stream_ended is True
        assert d.turn_active is False
        assert [e.kind for e in seen if isinstance(e, TurnFailure)] == ["process_crash"]
        assert client.queries == 2
        await d.aclose()
        task.cancel()
    asyncio.run(run())
