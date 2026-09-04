"""Every background loop ``start()`` launches must be done after ``stop()``.

``stop()`` cancels a hand-maintained list of task attributes. Two loops added
later — the recurring-session scheduler and the SDLC intake tick — were never
added to that list, so they outlived ``stop()`` as orphan tasks on the loop.
Under systemd that is invisible; under the test harness they survive the app
lifespan and are cancelled only by the runner's final loop shutdown — AFTER the
DB engine is disposed (#405's exact contamination vector, and a live suspect for
the CI teardown stalls tracked in #846).

Pinned by task NAME so the next loop someone adds to ``start()`` without adding
it to ``stop()`` fails here too.
"""
from __future__ import annotations

import asyncio

import pytest

from server.db import dispose_db, init_db
from server.tests.fakes.fake_factory import fake_sdk_factory


def _new_orch():
    from server.sessions.orchestrator import SessionOrchestrator
    # poll_interval huge so the usage poller / monitors never tick during the test.
    return SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0)


@pytest.mark.asyncio
async def test_stop_cancels_every_loop_that_start_created(tmp_path):
    await init_db(f"sqlite+aiosqlite:///{tmp_path / 'c.db'}")
    try:
        orch = _new_orch()
        before = set(asyncio.all_tasks())
        await orch.start()
        started = {t for t in asyncio.all_tasks() if t not in before}
        assert started, "start() created no tasks — harness assumption broken"

        await orch.stop()

        alive = sorted(t.get_name() for t in started if not t.done())
        assert not alive, f"loops still alive after stop(): {alive}"
    finally:
        await dispose_db()
