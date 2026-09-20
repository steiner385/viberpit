"""An abandoned ``bounded_wait`` phase must name its culprit and report its fate.

``server/aio.py`` exists so a wedged teardown phase costs seconds, not the process
(issue #846) — and so "the next hang names its culprit". ``reap_lingering_db_tasks``
honours the second half by logging ``format_task_dump`` of whatever is still pending;
``bounded_wait``, the module's primary abandonment path and the one that actually
fires in prod, did not. Its CRITICAL named the phase string and nothing else, so
15 occurrences of ``stall.interrupt:<sid>`` over 11 days (issue #1539) identified
nothing about where the task was parked — every candidate absorber on that path (the
SDK control request, the LLM helper lane, the asyncpg query) honours an external
cancel when probed alone, so the answer was only ever going to come from the live
await chain.

It also dropped the abandoned task on the floor: never consumed, no done callback.
So a phase that later raised surfaced as an unattributed "Task exception was never
retrieved", and a phase that merely unwound slowly was indistinguishable in the logs
from one genuinely stuck forever.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging

import pytest

from server.aio import bounded_wait


async def _parked_in_an_identifiable_frame(gate: asyncio.Event) -> None:
    """Park in a frame whose function name is greppable in the dump."""
    await gate.wait()


async def _swallows_every_cancel(gate: asyncio.Event) -> str:
    """The absorb-layer shape: re-park instead of unwinding on a cancel."""
    while True:
        try:
            await gate.wait()
            return "finished"
        except asyncio.CancelledError:
            continue


@contextlib.asynccontextmanager
async def released_on_exit():
    """Yield a gate and release it on the way out, however the test ends.

    Releasing inline at the end of a test body is not enough: a failing assertion
    skips the release, the cancel-immune task stays parked, and the loop's own
    ``_cancel_all_tasks`` — which delivers exactly ONE cancel and then gathers
    unboundedly — wedges. The suite would HANG instead of reporting the failure,
    which is the very absorb-layer wedge ``reap_lingering_db_tasks`` documents,
    reproduced in our own teardown. Release must also happen while the loop is
    still running, so this is an async CM, not a sync fixture.
    """
    gate = asyncio.Event()
    try:
        yield gate
    finally:
        gate.set()
        for _ in range(20):          # let the abandoned task settle before loop close
            await asyncio.sleep(0)


def _records(caplog, level: int) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno == level and r.name == "server.aio"]


@pytest.mark.asyncio
async def test_abandoned_phase_dump_names_the_parked_frame(caplog):
    """The CRITICAL must carry the task's await chain, not just the phase string."""
    async with released_on_exit() as gate:
        with caplog.at_level(logging.CRITICAL, logger="server.aio"):
            ok = await bounded_wait(_swallows_every_cancel(gate),
                                    timeout=0.05, phase="probe", cancel_grace=0.05)
        assert ok is False
        crit = _records(caplog, logging.CRITICAL)
        assert len(crit) == 1
        msg = crit[0].getMessage()
        assert "probe" in msg
        # The whole point: the frame the task is parked in, by name and by file.
        assert "_swallows_every_cancel" in msg
        assert "test_aio_bounded_wait_abandon.py" in msg


@pytest.mark.asyncio
async def test_abandoned_phase_that_finishes_later_is_reported_as_slow(caplog):
    """A phase that completes after abandonment was slow, not stuck — say so.

    This is the readout that makes the recurring CRITICAL falsifiable: a companion
    WARNING that always says "completed" means the budget is too small, not that
    anything is wedged.
    """
    async with released_on_exit() as gate:
        with caplog.at_level(logging.DEBUG, logger="server.aio"):
            ok = await bounded_wait(_swallows_every_cancel(gate),
                                    timeout=0.05, phase="slow-phase", cancel_grace=0.05)
            assert ok is False
            gate.set()
            for _ in range(100):
                await asyncio.sleep(0.01)
                if _records(caplog, logging.WARNING):
                    break

        warn = [r.getMessage() for r in _records(caplog, logging.WARNING)]
        assert any("slow-phase" in m and "completed" in m for m in warn), warn


@pytest.mark.asyncio
async def test_abandoned_phase_failure_is_retrieved_and_attributed(caplog):
    """An abandoned task's exception must be consumed and logged against its phase.

    Left unretrieved, asyncio's default handler prints "Task exception was never
    retrieved" with no phase name attached — noise nobody can route back here.
    """
    async with released_on_exit() as gate:
        async def fails_after_abandonment() -> None:
            while True:
                try:
                    await gate.wait()
                    raise RuntimeError("boom")
                except asyncio.CancelledError:
                    continue

        task = asyncio.ensure_future(fails_after_abandonment())
        with caplog.at_level(logging.DEBUG, logger="server.aio"):
            ok = await bounded_wait(task, timeout=0.05, phase="doomed", cancel_grace=0.05)
            assert ok is False
            gate.set()
            for _ in range(100):
                await asyncio.sleep(0.01)
                if task.done():
                    break

        assert task.done()
        warn = [r.getMessage() for r in _records(caplog, logging.WARNING)]
        assert any("doomed" in m for m in warn), warn
        assert isinstance(task.exception(), RuntimeError)


@pytest.mark.asyncio
async def test_completed_phase_logs_nothing_and_returns_true(caplog):
    """The happy path must stay silent — this fires on every teardown."""
    async def quick() -> None:
        return None

    with caplog.at_level(logging.DEBUG, logger="server.aio"):
        assert await bounded_wait(quick(), timeout=1.0, phase="quick") is True
    assert not caplog.records


@pytest.mark.asyncio
async def test_phase_that_unwinds_on_cancel_is_not_abandoned(caplog):
    """A well-behaved phase blows its budget but unwinds — no CRITICAL, no dump."""
    async with released_on_exit() as gate:
        with caplog.at_level(logging.DEBUG, logger="server.aio"):
            ok = await bounded_wait(_parked_in_an_identifiable_frame(gate),
                                    timeout=0.05, phase="polite", cancel_grace=1.0)
        assert ok is False
        assert not _records(caplog, logging.CRITICAL)
        assert not _records(caplog, logging.WARNING)


@pytest.mark.asyncio
async def test_abandonment_still_costs_only_the_budget(caplog):
    """The contract that started #846: bounded_wait returns, whatever the phase does."""
    async with released_on_exit() as gate:
        loop = asyncio.get_running_loop()
        start = loop.time()
        with caplog.at_level(logging.CRITICAL, logger="server.aio"):
            await bounded_wait(_swallows_every_cancel(gate),
                               timeout=0.1, phase="wedged", cancel_grace=0.1)
        elapsed = loop.time() - start
        assert elapsed < 2.0, f"bounded_wait took {elapsed:.2f}s on a cancel-immune phase"
