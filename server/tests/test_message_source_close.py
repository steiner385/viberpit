"""The SDK cursor's close is OWNED — never blanket-suppressed, never dropped (#1869, #1870).

Closing the SDK's message stream is allowed to raise: its transport catches
``GeneratorExit`` and then re-raises the CLI's non-zero exit from its own
``finally``. A generator we abandon instead of closing is finalized by asyncio's
GC hook in a task nobody holds, and that is how both shapes reached the daemon's
journal as an unattributed "Task exception was never retrieved":

* ``ProcessError('Command failed with exit code 1')`` (#1869)
* ``RuntimeError('aclose(): asynchronous generator is already running')`` (#1870)
"""
import asyncio
import logging

from server.sessions.claude_driver import _EOF, _ORPHAN_CURSOR_CLOSERS, _MessageSource


class _Client:
    """Minimal stand-in for the SDK client: one persistent message generator."""

    def __init__(self, agen):
        self._agen = agen

    def receive_messages(self):
        return self._agen


class _Tracking:
    """Wraps a real async generator so the test can see WHEN aclose() was called."""

    def __init__(self, agen):
        self._agen = agen
        self.close_calls = 0

    def __anext__(self):
        return self._agen.__anext__()

    async def aclose(self):
        self.close_calls += 1
        await self._agen.aclose()

    @property
    def spent(self) -> bool:
        return self._agen.ag_frame is None


async def _raises_on_close():
    try:
        yield {"type": "system"}
    except GeneratorExit:
        pass
    raise RuntimeError("Command failed with exit code 1")


def test_a_close_that_raises_is_retrieved_and_attributed(caplog):
    caplog.set_level(logging.DEBUG)

    async def main():
        src = _MessageSource(_Client(_raises_on_close()))
        assert await src.next() == {"type": "system"}
        await src.aclose(0.5, phase="driver.source:s1")   # must not raise
        # The cursor is retired: a later consumer sees EOF, not an AttributeError.
        assert await src.next() is _EOF

    asyncio.run(main())

    # bounded_wait retrieved the failure and logged it AGAINST ITS PHASE, instead of
    # leaving it to surface later as an ownerless asyncio ERROR.
    logged = [r.getMessage() for r in caplog.records]
    assert any("driver.source:s1:cursor-close" in m for m in logged), logged


def test_a_running_cursor_is_closed_by_us_once_its_frame_is_free():
    """The #1870 shape: an ``__anext__`` that outlives its own cancellation.

    Closing there raises "asynchronous generator is already running"; dropping the
    reference hands it to the GC hook. Neither — we keep it and close it later.
    """
    released = asyncio.Event()
    state: dict = {}

    async def stubborn():
        try:
            while True:
                try:
                    await released.wait()
                    return
                except asyncio.CancelledError:
                    continue          # cancel-immune, exactly as in #846
            yield None                # unreachable — but it is what makes this an
            #                           async generator rather than a coroutine
        finally:
            state["unwound"] = True

    async def main():
        tracking = _Tracking(stubborn())
        src = _MessageSource(_Client(tracking))
        before = set(_ORPHAN_CURSOR_CLOSERS)
        pump = asyncio.ensure_future(src.next())
        await asyncio.sleep(0.01)                     # park inside __anext__
        pump.cancel()
        await src.aclose(0.05, phase="driver.source:s2")
        # Not closed inline — the frame was still running, so aclose() would have
        # raised. Ownership was taken instead.
        assert tracking.close_calls == 0
        reapers = set(_ORPHAN_CURSOR_CLOSERS) - before
        assert len(reapers) == 1
        released.set()                                # the stream finally unwinds
        await asyncio.wait(reapers, timeout=2.0)
        assert tracking.close_calls == 1
        assert tracking.spent is True
        assert state.get("unwound") is True
        # And the reaper deregisters itself — the set is a handle, not a leak.
        assert not (set(_ORPHAN_CURSOR_CLOSERS) & reapers)

    asyncio.run(main())


def test_an_idle_cursor_closes_inline():
    """The ordinary path stays inline: nothing detached, nothing left running."""

    async def main():
        tracking = _Tracking(_plain())
        src = _MessageSource(_Client(tracking))
        assert await src.next() == 1
        before = set(_ORPHAN_CURSOR_CLOSERS)
        await src.aclose(0.5, phase="driver.source:s3")
        assert tracking.close_calls == 1
        assert tracking.spent is True
        assert set(_ORPHAN_CURSOR_CLOSERS) == before

    asyncio.run(main())


async def _plain():
    yield 1
    yield 2
