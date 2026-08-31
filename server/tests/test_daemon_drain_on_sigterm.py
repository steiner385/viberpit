"""The daemon drains its own in-flight turns on SIGTERM (2026-08-30 incident).

Every ``systemctl stop/restart ccswitch-daemon.service`` — deploy controller, wedge
watchdog, an operator or an agent typing it after an env edit — is the same SIGTERM.
Only the deploy controller drained first, over RPC, and on a host whose daemon binds
https-only that RPC can't even connect. So five sessions died mid-turn at 21:06 when a
background job restarted the daemon to pick up a ``daemon.env`` change.

``install_drain_on_sigterm`` makes the daemon protect its children itself: SIGTERM
enters drain mode (refuse new turns, snapshot pending work, tell the UI), waits with
the SAME caps as the deploy drain while the control plane keeps serving, then hands
the signal to whatever handler uvicorn installed so the ordinary graceful shutdown
runs. A second SIGTERM skips the wait.
"""
from __future__ import annotations

import asyncio
import functools
import os
import signal
import threading

import pytest

from server.deploy.drain import drain_and_wait
from server.ipc import drain_on_signal as mod

SIG = signal.SIGTERM


class FakeOrchestrator:
    def __init__(self, active: int, condensing: int = 0):
        self.active = active
        self.condensing = condensing
        self.begin_calls = 0
        self._draining = False

    async def begin_drain(self):
        self.begin_calls += 1
        self._draining = True
        return self.drain_status()

    def drain_status(self):
        state = "idle" if not self._draining else ("ready" if self.active == 0 else "draining")
        return {"state": state, "busySessions": self.active, "condensing": self.condensing}

    def active_session_count(self):
        return self.active


@pytest.fixture
def previous_handler():
    """Stand in for uvicorn's ``handle_exit``: record every hand-off, and restore the
    original disposition after the test no matter what."""
    calls: list[tuple[int, object]] = []
    original = signal.getsignal(SIG)
    signal.signal(SIG, lambda signum, frame: calls.append((signum, frame)))
    try:
        yield calls
    finally:
        signal.signal(SIG, original if original is not None else signal.SIG_DFL)


def _fast_drain(**caps):
    return functools.partial(drain_and_wait, poll_s=0.01, **caps)


async def _wait_for(pred, timeout=5.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not pred():
        if loop.time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.01)


def _raise_sigterm():
    # The handler must be OURS before we signal the test process, or pytest dies.
    assert signal.getsignal(SIG) is not signal.SIG_DFL
    os.kill(os.getpid(), SIG)


@pytest.mark.asyncio
async def test_sigterm_enters_drain_and_hands_off_only_once_turns_finish(previous_handler):
    orch = FakeOrchestrator(active=2)
    uninstall = mod.install_drain_on_sigterm(orch, drain=_fast_drain(turn_cap_s=5))
    try:
        _raise_sigterm()
        await _wait_for(lambda: orch.begin_calls == 1)
        await asyncio.sleep(0.05)
        assert previous_handler == [], "must not shut down while turns are in flight"
        orch.active = 0
        await _wait_for(lambda: previous_handler)
        assert previous_handler == [(SIG, None)]
    finally:
        uninstall()


@pytest.mark.asyncio
async def test_idle_daemon_hands_off_immediately(previous_handler):
    orch = FakeOrchestrator(active=0)
    uninstall = mod.install_drain_on_sigterm(orch, drain=_fast_drain(turn_cap_s=5))
    try:
        _raise_sigterm()
        await _wait_for(lambda: previous_handler)
        assert orch.begin_calls == 1
    finally:
        uninstall()


@pytest.mark.asyncio
async def test_turn_cap_bounds_the_wait(previous_handler):
    orch = FakeOrchestrator(active=1)          # never finishes
    uninstall = mod.install_drain_on_sigterm(orch, drain=_fast_drain(turn_cap_s=0.05))
    try:
        _raise_sigterm()
        await _wait_for(lambda: previous_handler)
    finally:
        uninstall()


@pytest.mark.asyncio
async def test_second_sigterm_skips_the_wait(previous_handler):
    orch = FakeOrchestrator(active=1)          # never finishes
    uninstall = mod.install_drain_on_sigterm(orch, drain=_fast_drain(turn_cap_s=60))
    try:
        _raise_sigterm()
        await _wait_for(lambda: orch.begin_calls == 1)
        assert previous_handler == []
        _raise_sigterm()
        await _wait_for(lambda: previous_handler)
        await asyncio.sleep(0.05)
        assert len(previous_handler) == 1, "hand off exactly once"
    finally:
        uninstall()


@pytest.mark.asyncio
async def test_drain_failure_still_hands_off(previous_handler):
    orch = FakeOrchestrator(active=1)

    async def broken(client):
        raise RuntimeError("boom")

    uninstall = mod.install_drain_on_sigterm(orch, drain=broken)
    try:
        _raise_sigterm()
        await _wait_for(lambda: previous_handler)
    finally:
        uninstall()


@pytest.mark.asyncio
async def test_uninstall_restores_the_previous_handler(previous_handler):
    orch = FakeOrchestrator(active=0)
    before = signal.getsignal(SIG)
    uninstall = mod.install_drain_on_sigterm(orch)
    assert signal.getsignal(SIG) is not before
    uninstall()
    assert signal.getsignal(SIG) is before
    uninstall()   # idempotent


@pytest.mark.asyncio
async def test_off_main_thread_is_a_noop(previous_handler):
    """TestClient-style lifespans run off the main thread, where signal handlers can't
    be installed — that must degrade to a no-op, never an exception."""
    orch = FakeOrchestrator(active=0)
    before = signal.getsignal(SIG)
    loop = asyncio.get_running_loop()
    result: dict = {}

    def run():
        try:
            result["uninstall"] = mod.install_drain_on_sigterm(orch, loop=loop)
        except Exception as exc:   # pragma: no cover — the assertion below reports it
            result["exc"] = exc

    t = threading.Thread(target=run)
    t.start()
    t.join()
    assert "exc" not in result, result.get("exc")
    assert signal.getsignal(SIG) is before
    result["uninstall"]()


def test_local_client_adapts_orchestrator_drain_calls():
    orch = FakeOrchestrator(active=1)
    client = mod.LocalDrainClient(orch)
    assert asyncio.run(client.call("begin_drain"))["state"] == "draining"
    orch.active = 0
    assert asyncio.run(client.call("drain_status"))["state"] == "ready"
    with pytest.raises(Exception):
        asyncio.run(client.call("list_sessions"))
