"""The stall watchdog's interrupt of a wedged turn must be bounded.

Sibling of the _reassign fix in test_reassign_interrupt_timeout.py (see
docs/superpowers/fix-patterns/2026-09-03-unbounded-control-request-in-supervisor-path.md).
``_check_stalls`` loops over EVERY live driver; an unbounded ``await driver.interrupt()`` on a
CLI that ignores the control channel blocked the whole tick for the SDK's 60 s and skipped the
stall check of every later driver (and the disabled-account sweep) for that tick.
"""
import asyncio

import pytest

from server.sessions import orchestrator as orch_mod
from server.sessions.orchestrator import SessionOrchestrator


class _Driver:
    def __init__(self, account, *, hang):
        self.account = account
        self._turn_active = True
        self.drain_alive = False
        self.in_api_backoff = False
        self.awaiting_user_input = False
        self.last_progress_at = 0.0
        self.interrupted = False
        self._hang = hang

    async def interrupt(self):
        if self._hang:
            await asyncio.Event().wait()
        self.interrupted = True


@pytest.mark.asyncio
async def test_wedged_interrupt_does_not_block_the_rest_of_the_tick(monkeypatch):
    monkeypatch.setattr(orch_mod, "_CLOSE_GRACE_SEC", 0.05)
    o = SessionOrchestrator(sdk_factory=lambda *a, **k: None)
    o._clock = lambda: 1000.0
    wedged = _Driver("a", hang=True)
    healthy = _Driver("b", hang=False)
    o._drivers["wedged"] = wedged
    o._drivers["late"] = healthy

    await asyncio.wait_for(o._check_stalls(1000.0), timeout=5.0)

    assert healthy.interrupted, "the driver after the wedged one was never reached"
    assert wedged.interrupted is False
