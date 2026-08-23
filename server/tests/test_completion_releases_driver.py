"""Completion releases the driver — deterministically, no autopilot dependency.

Regression: a session reaching `complete`/`done` never had its driver popped —
only `close_session`, `archive_session`, and daemon `stop()` did. Auto-archive was
the ONLY path that freed that resource, and it had two independent failure modes
(autopilot outages; the sticky manual-unarchive opt-out), so the capacity cap could
fill with finished-but-unarchived sessions and deadlock the queue forever.

`_persist_status` now pops + closes the driver itself on the `complete`/`done`
transition (see orchestrator.py, same pop+suppress+aclose idiom as
close_session/stop()). Nothing is archived: the row and transcript stay, and
reopening lazily rebuilds the driver via `ensure_driver` (resume=claude_session_id).
`idle` is untouched — an idle session is alive and awaiting input.

The pop is synchronous (capacity accounting sees it the instant `_persist_status`
returns) but the `aclose()` await is deferred to its own spawned task, not awaited
inline: `_persist_status` runs AS the driver's own `on_status` hook (called from
inside its pump task), and `aclose()` awaits that same pump task to finish — awaiting
it inline would be the task waiting on itself. Tests below drain the event loop
after `_persist_status` to let that spawned task run before asserting on it.
"""
from __future__ import annotations

import asyncio

from server.db import db_session, init_db
from server.db.models import Session as DbSession
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory


class _FakeDriver:
    """Minimal driver stub exposing an awaitable ``aclose()``.

    ``claude_session_id``/``last_pr_resolution`` are attributes ``_persist_status``
    and ``transcript_path`` read via ``getattr``/truthiness checks on any live
    driver — None makes both fall through to their detached-session fallback.
    """

    claude_session_id = None
    last_pr_resolution = None

    def __init__(self, *, raise_on_close: bool = False):
        self.raise_on_close = raise_on_close
        self.closed = False

    async def aclose(self):
        self.closed = True
        if self.raise_on_close:
            raise RuntimeError("teardown blew up")


async def _drain():
    # Let the spawned driver-release task (see module docstring) run to completion.
    for _ in range(4):
        await asyncio.sleep(0)


async def _build_orch(tmp_path, **row):
    db_url = f"sqlite+aiosqlite:///{tmp_path/'ccswitch.db'}"
    await init_db(db_url=db_url)
    base = dict(id="s1", name="New Session", account_name="acct1",
                claude_session_id="claude-s1", status="in_progress", cwd="/repo/s1")
    base.update(row)
    async with db_session() as db:
        db.add(DbSession(**base))
    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
    await orch.rehydrate()
    return orch


def test_complete_status_pops_and_closes_driver(tmp_path):
    """Reaching `complete` releases the driver on its own — deterministically, not
    via auto-archive. The record carries the sticky manual-unarchive opt-out (the
    realistic state of 6 of the 7 deadlocked sessions) so the pre-existing
    auto-archive path is a guaranteed no-op here: any release we observe is proven
    to come from THIS fix, and the assertions below confirm nothing was archived."""
    async def run():
        orch = await _build_orch(tmp_path)
        orch.record("s1").auto_archive_opt_out_reason = "manual-unarchive"
        driver = _FakeDriver()
        orch._drivers["s1"] = driver
        await orch._persist_status("s1", "complete")
        assert "s1" not in orch._drivers  # pop is synchronous
        await _drain()
        return orch, driver
    orch, driver = asyncio.run(run())
    assert driver.closed is True
    assert "s1" not in orch._drivers
    # Non-negotiable: nothing gets archived. The row, and its `complete` status
    # (not `archived`), stay exactly as completion left them.
    assert orch.record("s1").status == "complete"


def test_complete_status_does_not_archive_the_session(tmp_path):
    async def run():
        orch = await _build_orch(tmp_path)
        orch.record("s1").auto_archive_opt_out_reason = "manual-unarchive"
        orch._drivers["s1"] = _FakeDriver()
        await orch._persist_status("s1", "complete")
        async with db_session() as db:
            from sqlalchemy import select as sa_select
            row = (await db.execute(
                sa_select(DbSession).where(DbSession.id == "s1"))).scalars().first()
        return row.status
    status = asyncio.run(run())
    assert status == "complete"


def test_done_status_pops_and_closes_driver(tmp_path):
    async def run():
        orch = await _build_orch(tmp_path)
        driver = _FakeDriver()
        orch._drivers["s1"] = driver
        await orch._persist_status("s1", "done")
        assert "s1" not in orch._drivers  # pop is synchronous
        await _drain()
        return driver
    driver = asyncio.run(run())
    assert driver.closed is True


def test_idle_status_keeps_driver(tmp_path):
    """idle is alive and awaiting input — it must keep its driver + capacity slot."""
    async def run():
        orch = await _build_orch(tmp_path)
        driver = _FakeDriver()
        orch._drivers["s1"] = driver
        await orch._persist_status("s1", "idle")
        assert "s1" in orch._drivers
        assert orch._drivers["s1"] is driver
        return driver
    driver = asyncio.run(run())
    assert driver.closed is False


def test_teardown_failure_still_removes_driver_from_registry(tmp_path):
    """A driver whose aclose() raises must still be removed from _drivers — otherwise
    a failing teardown silently reintroduces the leak this fix exists to close."""
    async def run():
        orch = await _build_orch(tmp_path)
        orch.record("s1").auto_archive_opt_out_reason = "manual-unarchive"
        driver = _FakeDriver(raise_on_close=True)
        orch._drivers["s1"] = driver
        # Must not raise out of _persist_status, nor out of the spawned release task —
        # mirrors contextlib.suppress(Exception) around the aclose() call in the shared
        # close_session/stop() teardown idiom.
        await orch._persist_status("s1", "complete")
        assert "s1" not in orch._drivers  # pop is synchronous — happens either way
        await _drain()
        return driver
    driver = asyncio.run(run())
    assert driver.closed is True
