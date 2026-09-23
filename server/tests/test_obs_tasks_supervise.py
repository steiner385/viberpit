"""Regression (#1690): a supervised task must not be garbage-collected mid-run.

asyncio keeps only WEAK references to tasks. A pump/batcher parked on
``queue.get()`` is reachable only through its queue's getter future; once the
driver (or socket handler) owning that queue is dropped, the whole cycle is
garbage. The GC then logs "Task was destroyed but it is pending!", closes the
coroutine — whose GeneratorExit cancels the getter future, scheduling one more
``__step`` — and that step's ``coro.send`` raises a frame-less
``RuntimeError: cannot reuse already awaited coroutine``, which ``supervise``
reported as ``supervised task pump-<id> failed``.
"""
import asyncio
import gc
import logging

import pytest

from server.obs import tasks as mod
from server.obs.tasks import supervise


async def _park_on_private_queue() -> None:
    # The queue is referenced ONLY by this coroutine's frame, exactly like a pump
    # whose driver has been dropped.
    await asyncio.Queue().get()


@pytest.mark.asyncio
async def test_unreferenced_supervised_task_survives_gc(caplog):
    failures: list[BaseException] = []
    supervise(asyncio.create_task(_park_on_private_queue(), name="parked"),
              name="parked", on_failure=failures.append)
    await asyncio.sleep(0)  # let it park on get()

    with caplog.at_level(logging.ERROR):
        gc.collect()
        for _ in range(3):
            await asyncio.sleep(0)

    assert failures == []
    assert "destroyed but it is pending" not in caplog.text
    parked = [t for t in asyncio.all_tasks() if t.get_name() == "parked"]
    assert len(parked) == 1 and not parked[0].done()
    parked[0].cancel()
    await asyncio.gather(parked[0], return_exceptions=True)


@pytest.mark.asyncio
async def test_finished_task_reference_is_released():
    async def _quick() -> int:
        return 1

    t = supervise(asyncio.create_task(_quick()), name="quick")
    assert t in mod._live
    await t
    await asyncio.sleep(0)  # done-callbacks run on the next loop pass
    assert t not in mod._live
