"""``caused_by_cancellation`` / ``current_task_cancelling`` (#1950, #2751, #2752).

asyncpg/uvloop catch a cancellation mid TLS handshake and re-raise a concrete
exception ``from`` it, so an ``except Exception`` site sees what looks like a real
DB failure. These helpers are the single classifier for that shape.
"""
import asyncio

from server.aio import (
    caused_by_cancellation, current_task_cancelling, ended_by_wrapped_cancellation,
)


def test_caused_by_cancellation_detects_bare():
    assert caused_by_cancellation(asyncio.CancelledError()) is True


def test_caused_by_cancellation_detects_chained_cause():
    """asyncpg/uvloop pattern: ``raise ConcreteError(...) from cancelled_error``."""
    try:
        try:
            raise asyncio.CancelledError()
        except asyncio.CancelledError as cancelled:
            raise ConnectionResetError("connection lost") from cancelled
    except ConnectionResetError as exc:
        assert caused_by_cancellation(exc) is True


def test_caused_by_cancellation_detects_implicit_context():
    try:
        try:
            raise asyncio.CancelledError()
        except asyncio.CancelledError:
            raise ConnectionResetError("connection lost")  # noqa: B904 (intentional)
    except ConnectionResetError as exc:
        assert caused_by_cancellation(exc) is True


def test_caused_by_cancellation_false_for_unrelated_error():
    assert caused_by_cancellation(ValueError("nope")) is False


def test_caused_by_cancellation_self_referential_chain_terminates():
    """A cycle in __context__ must not recurse forever."""
    exc = ValueError("cycle")
    exc.__context__ = exc
    assert caused_by_cancellation(exc) is False



def test_current_task_cancelling_tracks_a_wrapped_cancel_request():
    """A driver that swallows our CancelledError and raises something else must
    still leave the cancel request visible — that is how callers tell OUR
    cancellation (propagate) from a driver-internal one (degrade)."""
    seen: list[bool] = []

    async def victim(started: asyncio.Event):
        seen.append(current_task_cancelling())
        started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError as cancelled:
            try:
                raise ConnectionResetError("handshake aborted") from cancelled
            except ConnectionResetError:
                seen.append(current_task_cancelling())

    async def go():
        started = asyncio.Event()
        task = asyncio.create_task(victim(started))
        await started.wait()
        task.cancel()
        await task

    asyncio.run(go())
    assert seen == [False, True]


def _finished(*, cancel: bool, wrap: bool) -> asyncio.Task:
    async def victim(started: asyncio.Event):
        started.set()
        try:
            await asyncio.sleep(10 if cancel else 0)
            raise asyncio.CancelledError()
        except asyncio.CancelledError as cancelled:
            if wrap:
                raise ConnectionResetError("handshake aborted") from cancelled
            raise

    async def go():
        started = asyncio.Event()
        task = asyncio.create_task(victim(started))
        await started.wait()
        if cancel:
            task.cancel()
        await asyncio.wait({task})
        return task

    return asyncio.run(go())


def test_ended_by_wrapped_cancellation_true_for_a_cancelled_task_the_driver_wrapped():
    assert ended_by_wrapped_cancellation(_finished(cancel=True, wrap=True)) is True


def test_ended_by_wrapped_cancellation_false_without_cancel_on_the_task():
    assert ended_by_wrapped_cancellation(_finished(cancel=False, wrap=True)) is False


def test_ended_by_wrapped_cancellation_false_for_a_plain_cancelled_task():
    task = _finished(cancel=True, wrap=False)
    assert task.cancelled()
    assert ended_by_wrapped_cancellation(task) is False
