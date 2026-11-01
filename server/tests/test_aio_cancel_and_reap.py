"""Issue #1164: reaping a cancelled child must never swallow the CALLER's cancellation.

The bug shape these tests lock down::

    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):   # ← swallows the OUTER cancel too
        pass

After ``task.cancel()`` the ``CancelledError`` surfacing from ``await task`` is *usually*
the child's — but when the surrounding coroutine is itself being cancelled (exactly what
the SIGTERM drain does, ``server/ipc/drain_on_signal.py``) the same handler eats the outer
cancellation and the caller keeps running as though it were never cancelled. That is how a
graceful shutdown overruns its ``TimeoutStopSec`` and gets SIGKILLed with live sessions
attached.

``cancel_and_reap`` resolves the ambiguity structurally rather than by heuristic:
``asyncio.wait`` absorbs the child's cancellation into the task object, so any
``CancelledError`` escaping it belongs to the caller and must propagate.
"""
import asyncio

import pytest

from server.aio import cancel_and_reap


async def _forever() -> None:
    await asyncio.sleep(3600)


async def _ignores_first_cancel(release: asyncio.Event) -> None:
    """Swallow the first cancellation and stay parked until *release* is set.

    Models an uncancellable teardown region (a `finally` awaiting a flush, the
    aiosqlite close future in #846) — the caller must not be held hostage by it.
    """
    try:
        await asyncio.sleep(3600)
    except asyncio.CancelledError:
        await release.wait()


def test_cancels_the_child_and_reports_it_reaped():
    async def main():
        child = asyncio.create_task(_forever())
        await asyncio.sleep(0)
        assert await cancel_and_reap(child, phase="test") is True
        assert child.cancelled()

    asyncio.run(main())


def test_outer_cancellation_propagates_instead_of_being_swallowed():
    """The regression: cancelling the CALLER while it reaps must unwind the caller."""
    async def main():
        release = asyncio.Event()
        child = asyncio.create_task(_ignores_first_cancel(release))
        reached_end = False
        reaping = asyncio.Event()

        async def caller():
            nonlocal reached_end
            reaping.set()
            await cancel_and_reap(child, phase="test")
            reached_end = True

        task = asyncio.create_task(caller())
        await reaping.wait()
        await asyncio.sleep(0.05)  # let it park inside the reap
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled()
        assert reached_end is False, "outer cancellation was swallowed by the reap"

        release.set()
        await asyncio.wait({child}, timeout=1)

    asyncio.run(main())


def test_timeout_abandons_a_child_that_ignores_the_cancel():
    async def main():
        release = asyncio.Event()
        child = asyncio.create_task(_ignores_first_cancel(release))
        await asyncio.sleep(0)

        assert await cancel_and_reap(child, phase="test", timeout=0.05) is False
        assert not child.done()

        release.set()
        await asyncio.wait({child}, timeout=1)

    asyncio.run(main())


def test_child_failure_is_retrieved_not_raised():
    """A failed child must be consumed here, not resurface as 'exception was never
    retrieved' noise on a later GC pass — and must never break the teardown path."""
    async def boom():
        raise RuntimeError("child blew up")

    async def main():
        child = asyncio.create_task(boom())
        await asyncio.sleep(0)
        assert await cancel_and_reap(child, phase="test") is True
        assert isinstance(child.exception(), RuntimeError)

    asyncio.run(main())


def test_none_is_a_noop():
    assert asyncio.run(cancel_and_reap(None, phase="test")) is True


def test_finished_task_is_not_cancelled():
    async def main():
        child = asyncio.create_task(asyncio.sleep(0))
        await child
        assert await cancel_and_reap(child, phase="test") is True
        assert not child.cancelled()

    asyncio.run(main())


def test_reaping_your_own_task_never_cancels_you():
    """Guard against the worst possible misuse: a teardown path cancelling itself."""
    async def main():
        me = asyncio.current_task()
        assert await cancel_and_reap(me, phase="test") is False
        assert me is not None and not me.cancelling()

    asyncio.run(main())


def test_does_not_re_cancel_a_task_already_being_cancelled():
    """A second cancel() ESCALATES: it interrupts cleanup the child started in
    response to the first one. Callers that cancel before reaping (the Codex
    manager cancels its supervisor, tears the process down, then reaps) must get
    the child's full unwind, not a truncated one."""
    async def main():
        cleaned_up = False

        async def cleans_up_on_cancel():
            nonlocal cleaned_up
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                await asyncio.sleep(0.05)   # its own teardown region
                cleaned_up = True
                raise

        child = asyncio.create_task(cleans_up_on_cancel())
        await asyncio.sleep(0)
        child.cancel()                      # the caller's own cancel, as in stop()
        await asyncio.sleep(0)

        assert await cancel_and_reap(child, phase="test") is True
        assert cleaned_up is True, "the reap escalated an in-flight cancellation"

    asyncio.run(main())


def test_reaps_a_child_whose_pending_cancel_was_an_absorbed_timeout():
    """#3203: an ``asyncio.timeout`` that has just expired inside the child ALSO reads
    as cancelling() == 1. The child's timeout then uncancel()s that request and carries
    on, so trusting the first "already being cancelled" verdict waited forever on a
    loop nobody had cancelled (the daemon's loop-lag watchdog, mid PSI read)."""
    async def main():
        async def absorbs_its_own_timeout():
            me = asyncio.current_task()
            assert me is not None
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                if me.uncancel() > 0:       # what Timeout.__aexit__ does on expiry
                    raise
            await asyncio.sleep(3600)       # absorbed as a TimeoutError — next tick

        child = asyncio.create_task(absorbs_its_own_timeout())
        await asyncio.sleep(0)
        child.cancel()                      # the expired timeout's request, not yet delivered

        assert await asyncio.wait_for(cancel_and_reap(child, phase="test"), 2) is True
        assert child.cancelled()

    asyncio.run(main())


def test_timeout_still_bounds_a_reap_that_keeps_rechecking():
    async def main():
        release = asyncio.Event()
        child = asyncio.create_task(_ignores_first_cancel(release))
        await asyncio.sleep(0)
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        assert await cancel_and_reap(child, phase="test", timeout=0.25) is False
        assert loop.time() - t0 < 1.0
        release.set()
        await asyncio.wait({child}, timeout=2)

    asyncio.run(main())
