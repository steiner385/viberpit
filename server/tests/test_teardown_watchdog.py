"""Tests for the teardown watchdog (issue #846).

The watchdog exists to name the coroutine that wedges TestClient teardown in
CI, so the core assertion is: given a task that never finishes, the dump
contains that task's NAME and its coroutine frames. And on the happy path it
must be silent and zero-cost.
"""
from __future__ import annotations

import asyncio
import io
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.tests.teardown_watchdog import TeardownWatchdog


def _loop_in_thread() -> tuple[asyncio.AbstractEventLoop, threading.Thread]:
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    return loop, thread


def _stop_loop(loop: asyncio.AbstractEventLoop, thread: threading.Thread) -> None:
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)
    loop.close()


async def _never_finishes() -> None:
    await asyncio.Event().wait()


def test_watchdog_dump_names_the_wedged_task():
    loop, thread = _loop_in_thread()
    try:
        async def _spawn() -> asyncio.Task:
            return asyncio.get_running_loop().create_task(
                _never_finishes(), name="wedged-simulated-task"
            )

        task = asyncio.run_coroutine_threadsafe(_spawn(), loop).result(timeout=5)
        buf = io.StringIO()
        with TeardownWatchdog(loop, timeout=0.2, label="unit-test", out=buf):
            time.sleep(1.0)  # let the timer fire and the loop-side dump land
        text = buf.getvalue()
        assert "teardown watchdog: unit-test exceeded" in text
        assert "wedged-simulated-task" in text
        assert "_never_finishes" in text  # coroutine frame, not just the name
        assert "loop is alive" in text    # dump ran on the loop itself
        loop.call_soon_threadsafe(task.cancel)
    finally:
        _stop_loop(loop, thread)


def test_watchdog_silent_and_cancelled_on_happy_path():
    loop, thread = _loop_in_thread()
    try:
        buf = io.StringIO()
        wd = TeardownWatchdog(loop, timeout=0.3, label="unit-test", out=buf)
        with wd:
            pass  # teardown "finished" instantly
        time.sleep(0.6)  # past the would-be deadline
        assert buf.getvalue() == ""
        assert not wd._timer.is_alive()
    finally:
        _stop_loop(loop, thread)


def test_watchdog_falls_back_to_cross_thread_dump_when_loop_blocked():
    loop, thread = _loop_in_thread()
    try:
        async def _spawn() -> asyncio.Task:
            return asyncio.get_running_loop().create_task(
                _never_finishes(), name="wedged-behind-blocked-loop"
            )

        task = asyncio.run_coroutine_threadsafe(_spawn(), loop).result(timeout=5)
        blocker_done = threading.Event()
        loop.call_soon_threadsafe(lambda: blocker_done.wait(2.5))  # block the loop
        buf = io.StringIO()
        with TeardownWatchdog(loop, timeout=0.2, label="unit-test", out=buf,
                              loop_dump_grace=0.3):
            time.sleep(1.2)
        blocker_done.set()
        text = buf.getvalue()
        assert "wedged-behind-blocked-loop" in text
        assert "cross-thread dump" in text
        loop.call_soon_threadsafe(task.cancel)
    finally:
        _stop_loop(loop, thread)


def test_testclient_exit_is_watched_end_to_end(monkeypatch, capfd):
    """The conftest wiring must arm the watchdog around a real TestClient exit.

    The app's lifespan shutdown deliberately takes ~1.2s while the watchdog is
    tuned to 0.3s via the env knob, so the dump fires — to the REAL stderr,
    which capfd sees — and teardown still completes (bounded test, no hang).
    """
    monkeypatch.setenv("CCSWITCH_TEARDOWN_WATCHDOG_S", "0.3")

    @asynccontextmanager
    async def lifespan(app):
        yield
        await asyncio.sleep(1.2)  # slow — but finite — shutdown

    app = FastAPI(lifespan=lifespan)
    with TestClient(app):
        pass

    err = capfd.readouterr().err
    assert "teardown watchdog: TestClient.__exit__ exceeded" in err
    # The lifespan task itself is what is awaited — its frames must be visible.
    assert "asyncio task(s) on the app loop" in err
    assert "lifespan" in err


def test_testclient_exit_watchdog_silent_when_fast(capfd):
    """Default 45s watchdog: a normal instant teardown must print nothing."""
    @asynccontextmanager
    async def lifespan(app):
        yield

    app = FastAPI(lifespan=lifespan)
    with TestClient(app):
        pass

    err = capfd.readouterr().err
    assert "teardown watchdog" not in err
