"""The SDK stream can die while the CLI process lives on — and the driver must treat
that as the crash it is, not as a quiet turn end.

Incident 2026-09-16 (session 1375c82f, dobby): the CLI echoed a 1,023,418-byte
``originalFile`` in an Edit result on stdout, the SDK's line reader tripped its 1 MiB
``max_buffer_size`` and died ("Fatal error in message reader"), and from then on the
daemon could WRITE to the CLI (prompts, interrupts) but never READ from it: no events,
no results, no permission requests. The driver logged "SDK stream ended while idle" as
a warning every couple of minutes and kept accepting prompts into that black hole; the
CLI parked on four unanswerable permission asks; the stall watchdog, seeing frozen
progress, interrupted every later turn within seconds and threw away the user's queued
"continue"; and ``_end_turn``'s context-usage pull blocked 60 s per turn on a control
request nobody would ever answer. Two hours of "Waiting…".

The contract pinned here: an unexpected end of the SDK cursor — the raised error frame
mid-turn, or a bare EOF while idle — signals ONE recoverable failure to the
orchestrator (which recomposes the driver), settles the turn promptly without issuing
control requests against the dead reader, and holds later prompts instead of writing
them to a process it can no longer hear.
"""
import asyncio
import logging

from server.obs.jsonlog import ContextFilter
from server.sessions.claude_driver import ClaudeAgentDriver
from server.sessions.events import ErrorEvent, TurnFailure
from server.sessions.permissions import PermissionRegistry
from server.tests.fakes import fake_sdk as f
from server.tests.fakes.fake_sdk import FakeSDKClient

_DIE = object()   # script marker: the reader dies HERE (the SDK raises its error frame)
_DRIVER_LOGGER = "server.sessions.claude_driver"


class _ReaderDiesClient(FakeSDKClient):
    """The SDK after its stdout reader died on an oversized line: ``receive_messages``
    raises once (the reader's ``{"type": "error"}`` frame) and is then finished for
    good — while the process, and therefore stdin, is still alive. Every control
    request issued afterwards would wait forever for a reply that cannot come; the
    fake makes that hang literal so a missing guard fails the test instead of
    passing by luck."""

    def __init__(self, messages, **kw):
        super().__init__(messages, **kw)
        self.dead = False
        self.control_calls: list[str] = []

    async def receive_messages(self):
        async for m in super().receive_messages():
            if m is _DIE:
                self.dead = True
                raise Exception(
                    "JSON message exceeded maximum buffer size of 1048576 bytes")
            yield m

    async def get_context_usage(self):
        self.control_calls.append("context_usage")
        if self.dead:
            await asyncio.Event().wait()
        return await super().get_context_usage()

    async def interrupt(self):
        self.control_calls.append("interrupt")
        if self.dead:
            await asyncio.Event().wait()
        await super().interrupt()


async def _wait_until(pred, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.005)


def _build(client, failures: list) -> ClaudeAgentDriver:
    async def on_fail(sid, kind, retry_after):
        failures.append((sid, kind, retry_after))
    return ClaudeAgentDriver("s-dead", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                             sdk_factory=lambda *a, **k: client, registry=PermissionRegistry(),
                             identity="me", on_recoverable_failure=on_fail)


def _collector(d: ClaudeAgentDriver):
    seen: list = []

    async def collect():
        async for e in d.events():
            seen.append(e)
    return seen, asyncio.create_task(collect())


def test_reader_death_mid_turn_is_a_crash_and_settles_without_control_requests():
    client = _ReaderDiesClient([
        f.tool_use("t1", "Edit", {"file_path": "server/sessions/orchestrator.py"}, uuid="a1"),
        _DIE,
    ])
    failures: list = []

    async def run():
        d = _build(client, failures)
        await d.start()
        seen, task = _collector(d)
        await d.send_prompt("go")
        # Typed while the turn was live: queued behind it, exactly like the user's
        # second "continue" in the incident.
        await d.send_prompt("early")
        await _wait_until(lambda: failures)
        # The turn must settle promptly — not after the SDK's 60 s control timeout.
        await _wait_until(lambda: not d.drain_alive, timeout=1.0)

        assert failures == [("s-dead", "process_crash", None)]
        assert d.turn_active is False
        assert d.stream_ended is True
        errs = [e for e in seen if isinstance(e, ErrorEvent)]
        assert errs and "maximum buffer size" in errs[0].message
        assert [e.kind for e in seen if isinstance(e, TurnFailure)] == ["process_crash"]
        # No control request went to the dead reader: not the context-usage pull
        # (the 60 s zombie), not the interrupt.
        assert client.control_calls == []
        # The queued prompt was NOT written into the void, and a new one is held too.
        assert client.sent == ["go"]
        await d.send_prompt("continue")
        assert client.sent == ["go"]
        assert d.queued() == ["early", "continue"]
        await d.interrupt(keep_queue=True)
        assert client.control_calls == []
        assert d.queued() == ["early", "continue"]
        # The end is signalled once: the listener that follows the settled turn
        # reads the same EOF and must not fire a second recovery.
        await asyncio.sleep(0.05)
        assert failures == [("s-dead", "process_crash", None)]
        await d.aclose()
        task.cancel()
    asyncio.run(run())


def test_stream_end_while_idle_signals_stream_lost_once():
    """A bare EOF with nobody closing the driver: the process exited, or its reader
    died between turns. Recovery kind ``stream_lost`` — recompose, but there is no
    interrupted prompt to replay."""
    client = FakeSDKClient([])
    failures: list = []

    async def run():
        d = _build(client, failures)
        await d.start()
        await asyncio.sleep(0.01)
        assert d.turn_active is False
        client.end_stream()
        await _wait_until(lambda: failures)
        assert failures == [("s-dead", "stream_lost", None)]
        assert d.stream_ended is True
        await asyncio.sleep(0.02)
        await d.aclose()
        await asyncio.sleep(0.02)
        assert failures == [("s-dead", "stream_lost", None)]
    asyncio.run(run())


def test_idle_stream_death_is_reported_once(caplog):
    """One stream death, one log record — the ``_on_stream_ended`` one.

    Before #1895 an idle EOF could only be complained about, so ``_idle_listen`` logged
    "SDK stream ended while idle" and returned into the black hole. #1895 gave the
    condition a real handler — ``_on_stream_ended``, which logs the same end at ERROR
    with ``mid_turn`` and the recompose it is about to do — but left the old warning
    behind, so one incident was reported twice. The error-triage pipeline fingerprints
    the normalized message, so the pair files a duplicate auto-error issue for a single
    stream death; #1943 is the WARNING half, on a fingerprint whose last occurrence
    (2026-09-17 13:18 UTC) predates its own fix (#1895, merged 13:36 UTC).

    The mid-turn sibling in ``_drain`` already had this right: it logs nothing of its
    own and lets ``_on_stream_ended`` own the record. Pinned here so a future second
    line fails instead of quietly re-filing.
    """
    client = FakeSDKClient([])
    failures: list = []

    async def run():
        d = _build(client, failures)
        await d.start()
        await asyncio.sleep(0.01)
        client.end_stream()
        await _wait_until(lambda: failures)
        await d.aclose()

    with caplog.at_level(logging.DEBUG, logger=_DRIVER_LOGGER):
        asyncio.run(run())

    deaths = [r for r in caplog.records
              if r.name == _DRIVER_LOGGER and "stream ended" in r.getMessage().lower()]
    assert len(deaths) == 1, [r.getMessage() for r in deaths]
    assert deaths[0].levelname == "ERROR"
    assert deaths[0].getMessage().startswith("driver.stream_ended:")
    assert "mid_turn=False" in deaths[0].getMessage()   # i.e. "while idle"


def test_clean_close_never_signals_recovery():
    """``aclose()`` ends the stream on purpose; that EOF is not a crash."""
    client = FakeSDKClient([])
    failures: list = []

    async def run():
        d = _build(client, failures)
        await d.start()
        await asyncio.sleep(0.01)
        await d.aclose()
        await asyncio.sleep(0.02)
        assert failures == []
        assert d.stream_ended is False
    asyncio.run(run())


def test_a_recoverable_drain_error_is_not_signalled_twice_by_the_eof_behind_it():
    """A crash the drain already classified (a killed CLI: exit code 143) is followed
    by the same EOF. One recovery, not two."""
    class _KilledClient(_ReaderDiesClient):
        async def receive_messages(self):
            async for m in FakeSDKClient.receive_messages(self):
                if m is _DIE:
                    self.dead = True
                    raise Exception("Command failed with exit code 143")
                yield m

    client = _KilledClient([f.tool_use("t1", "Bash", {"command": "ls"}, uuid="a1"), _DIE])
    failures: list = []

    async def run():
        d = _build(client, failures)
        await d.start()
        await d.send_prompt("go")
        await _wait_until(lambda: failures)
        await _wait_until(lambda: not d.drain_alive, timeout=1.0)
        await asyncio.sleep(0.05)
        assert failures == [("s-dead", "process_crash", None)]
        await d.aclose()
    asyncio.run(run())


def _stream_records(caplog) -> list[logging.LogRecord]:
    """Every WARNING+ record the driver logged — i.e. every one error triage files."""
    return [r for r in caplog.records
            if r.name == _DRIVER_LOGGER and r.levelno >= logging.WARNING]


def test_mid_turn_reader_death_is_reported_once_with_its_cause(caplog):
    """#1707: the raised error frame used to be logged twice — ``driver.drain`` (with
    the traceback) and then ``driver.stream_ended`` for the same loss — so error triage
    filed one incident as two issues (#1707 and #2331). The drain now logs nothing of
    its own; the one ``driver.stream_ended`` record carries the cause instead, so the
    "JSON message exceeded maximum buffer size" text is not lost with the traceback."""
    client = _ReaderDiesClient([f.tool_use("t1", "Bash", {"command": "ls"}, uuid="a1"), _DIE])
    failures: list = []

    async def run():
        d = _build(client, failures)
        await d.start()
        await d.send_prompt("go")
        await _wait_until(lambda: failures)
        await _wait_until(lambda: not d.drain_alive, timeout=1.0)
        await d.aclose()

    # The prod handlers stamp the session from the contextvar (ContextFilter); an
    # `extra=` or %s-only id never reaches the filed issue's "Sessions" row.
    caplog.handler.addFilter(ContextFilter())
    with caplog.at_level(logging.DEBUG, logger=_DRIVER_LOGGER):
        asyncio.run(run())

    recs = _stream_records(caplog)
    assert [r.getMessage().split(":")[0] for r in recs] == ["driver.stream_ended"], \
        [r.getMessage() for r in recs]
    assert "mid_turn=True" in recs[0].getMessage()
    assert "maximum buffer size" in recs[0].getMessage()
    assert recs[0].session_id == "s-dead"


def test_idle_reader_death_by_raised_error_recomposes(caplog):
    """#1885: the CLI exited between turns and the SDK surfaced it as a RAISED error
    frame ("Command failed with exit code 1"), not a bare EOF. ``_idle_listen`` only
    handled the EOF shape — the raise fell to its catch-all, which logged
    ``driver.idle_listen`` at ERROR and returned without marking the stream dead or
    signalling recovery: the pre-#1895 black hole, reopened by the other way a
    generator can end. Same contract as an idle EOF: one ``stream_lost``, one record."""
    client = _ReaderDiesClient([])
    failures: list = []

    async def run():
        d = _build(client, failures)
        await d.start()
        await asyncio.sleep(0.01)
        assert d.turn_active is False
        client._q().put_nowait(_DIE)
        await _wait_until(lambda: failures)
        assert failures == [("s-dead", "stream_lost", None)]
        assert d.stream_ended is True
        # Held, not written into a process nobody can hear.
        await d.send_prompt("continue")
        assert client.sent == []
        await asyncio.sleep(0.02)
        await d.aclose()
        assert failures == [("s-dead", "stream_lost", None)]

    with caplog.at_level(logging.DEBUG, logger=_DRIVER_LOGGER):
        asyncio.run(run())

    # The held prompt logs its own line (a separate, prompt-scoped condition); the
    # stream death itself must be exactly one record, and no `driver.idle_listen`.
    recs = [r for r in _stream_records(caplog)
            if not r.getMessage().startswith("driver.send_prompt")]
    assert [r.getMessage().split(":")[0] for r in recs] == ["driver.stream_ended"], \
        [r.getMessage() for r in recs]
    assert "mid_turn=False" in recs[0].getMessage()
    assert "maximum buffer size" in recs[0].getMessage()
