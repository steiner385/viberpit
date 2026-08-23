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
from unittest.mock import patch

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


async def _drain(orch):
    """Let every spawned background task — the driver-release task in particular
    (see module docstring) — run to completion. Deterministic (gather on the
    orchestrator's own tracked-task set) rather than a fixed sleep(0) count."""
    tasks = [t for t in orch._bg_tasks if not t.done()]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


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
        await _drain(orch)
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
        await _drain(orch)
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
        await _drain(orch)
        return driver
    driver = asyncio.run(run())
    assert driver.closed is True


# --- Review findings (Fix 1 + Fix 2) ---------------------------------------------
#
# Fix 1: a fast reopen must not race the old process's teardown. ensure_driver now
# awaits any in-flight completion-driver-release (tracked in orch._releasing) before
# building a replacement driver against the same claude_session_id/cwd — this race
# was unreachable before this task (a `complete` session always had a live driver),
# so it is a regression introduced by deferring aclose(), not a pre-existing gap.
#
# Fix 2: the five tests above all use a _FakeDriver.aclose() with no pump, so they
# pass identically whether _persist_status awaits aclose() inline or defers it via
# self._spawn(...) — they cannot catch a revert of that deferral. The tests below
# use a driver whose aclose() blocks forever (an Event that is never set) to prove
# _persist_status does NOT wait on it.


class _GatedCloseDriver(_FakeDriver):
    """aclose() blocks on ``self.gate`` until the test sets it — a stand-in for a
    real driver whose pump task is still shutting down."""

    def __init__(self, *, raise_on_close: bool = False):
        super().__init__(raise_on_close=raise_on_close)
        self.gate = asyncio.Event()

    async def aclose(self):
        await self.gate.wait()
        await super().aclose()


def test_persist_status_does_not_block_on_a_slow_aclose(tmp_path):
    """Regression guard for Fix 2: with aclose() correctly deferred to its own
    spawned task, _persist_status returns promptly even though this driver's
    aclose() never completes on its own. Verified this actually catches a revert:
    temporarily inlining `await live_driver.aclose()` in _persist_status (undoing
    the self._spawn(...) deferral) makes this test hit the 1s wait_for timeout and
    fail with asyncio.TimeoutError."""
    async def run():
        orch = await _build_orch(tmp_path)
        orch.record("s1").auto_archive_opt_out_reason = "manual-unarchive"
        driver = _GatedCloseDriver()
        orch._drivers["s1"] = driver
        # 1s is generous: the fix returns in well under a millisecond, since it
        # never awaits this driver's aclose() at all — only a broken (inline-await)
        # version would ever approach this timeout.
        await asyncio.wait_for(orch._persist_status("s1", "complete"), timeout=1)
        assert "s1" not in orch._drivers   # popped synchronously regardless
        assert driver.closed is False      # release is still blocked on the gate
        assert "s1" in orch._releasing     # Fix 1 bookkeeping: tracked in flight
        # Clean up: unblock the stuck release so nothing leaks past this test.
        driver.gate.set()
        await _drain(orch)
        return driver
    driver = asyncio.run(run())
    assert driver.closed is True


def test_ensure_driver_awaits_in_flight_release_before_rebuilding(tmp_path):
    """Fix 1: a fast reopen must not build a replacement driver while the prior
    process may still be flushing its session file to disk (the SDK's own
    subprocess_cli.py warns this can lose the last assistant message)."""
    async def run():
        cwd = str(tmp_path / "repo")
        orch = await _build_orch(tmp_path, cwd=cwd)
        # Opt out of auto-archive so it can't independently pop the record/driver
        # (see test_complete_status_pops_and_closes_driver) and contaminate this
        # test's isolation of the release-vs-reopen race.
        orch.record("s1").auto_archive_opt_out_reason = "manual-unarchive"
        driver = _GatedCloseDriver()
        orch._drivers["s1"] = driver
        await orch._persist_status("s1", "complete")
        assert "s1" not in orch._drivers
        assert "s1" in orch._releasing   # release in flight, gated

        with patch("server.sessions.orchestrator.compose", return_value=tmp_path / "cfg"), \
             patch("server.sessions.orchestrator.configdir.trust_project"):
            ensure_task = asyncio.create_task(orch.ensure_driver("s1"))
            # A real (if small) wall-clock bound, not a fixed scheduling-turn count:
            # the fake SDK's driver.start() has nothing else to block on, so if
            # ensure_driver were NOT awaiting the release, it would clear this in
            # well under 200ms regardless of how many turns that takes. Only the
            # gate (never set here) can make it still be pending.
            try:
                await asyncio.wait_for(asyncio.shield(ensure_task), timeout=0.2)
            except asyncio.TimeoutError:
                pass  # expected: still blocked on the in-flight release
            else:
                raise AssertionError(
                    "ensure_driver must wait for the in-flight release, but it "
                    "returned before the release's gate was ever set")
            assert driver.closed is False   # old process hasn't finished tearing down
            driver.gate.set()               # let the old release finish
            new_driver = await asyncio.wait_for(ensure_task, timeout=5)
        return driver, new_driver, orch
    driver, new_driver, orch = asyncio.run(run())
    assert driver.closed is True
    assert new_driver is not None
    assert new_driver is not driver
    assert "s1" not in orch._releasing   # cleared once the release completed


# --- Review findings (Task 3 follow-up: drain trigger) ---------------------------
#
# Task 3 exempted complete/done from the capacity count (CAPACITY_EXEMPT_STATUSES),
# but the drain trigger a few lines below the driver-release block in
# _persist_status still gated on the OLD set (INACTIVE_SESSION_STATUSES, i.e. only
# ended/archived). So a complete/done transition freed a slot in the count but
# nothing woke the queue for it — it would only drain on some later, unrelated
# event (another close/archive, a rehydrate, a settings change, a manual RPC).
# The trigger now reads CAPACITY_EXEMPT_STATUSES (excluding "queued" — see comment
# at the call site — since a session entering "queued" hasn't freed anything).


def test_complete_transition_schedules_a_queue_drain(tmp_path):
    """The whole point of Task 3: a complete transition must wake the queue
    immediately, not just free a slot that nothing drains until later."""
    async def run():
        orch = await _build_orch(tmp_path)
        orch._drivers["s1"] = _FakeDriver()
        drained = []

        async def _fake_maybe_start_queued():
            drained.append(True)
        orch._maybe_start_queued = _fake_maybe_start_queued
        await orch._persist_status("s1", "complete")
        await _drain(orch)
        return drained
    drained = asyncio.run(run())
    assert drained, "a complete transition must schedule _maybe_start_queued"


def test_done_transition_schedules_a_queue_drain(tmp_path):
    async def run():
        orch = await _build_orch(tmp_path)
        orch._drivers["s1"] = _FakeDriver()
        drained = []

        async def _fake_maybe_start_queued():
            drained.append(True)
        orch._maybe_start_queued = _fake_maybe_start_queued
        await orch._persist_status("s1", "done")
        await _drain(orch)
        return drained
    drained = asyncio.run(run())
    assert drained, "a done transition must schedule _maybe_start_queued"


def test_queued_status_does_not_self_trigger_a_drain(tmp_path):
    """Defensive guard: CAPACITY_EXEMPT_STATUSES includes "queued" (a session
    waiting FOR capacity, not one that just freed it). _persist_status is never
    actually called with status="queued" in production today — queued rows are
    written directly at creation/enqueue time, not via the driver's on_status
    callback — but the trigger excludes "queued" explicitly anyway so a future
    caller can't create a self-referential drain loop."""
    async def run():
        orch = await _build_orch(tmp_path)
        drained = []

        async def _fake_maybe_start_queued():
            drained.append(True)
        orch._maybe_start_queued = _fake_maybe_start_queued
        await orch._persist_status("s1", "queued")
        await _drain(orch)
        return drained
    drained = asyncio.run(run())
    assert not drained, "a queued transition must never self-trigger a drain"
