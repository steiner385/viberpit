"""Completion releases the driver — deterministically, no autopilot dependency.

Regression: a session reaching `complete` never had its driver popped — only
`close_session`, `archive_session`, and daemon `stop()` did. Auto-archive was the
ONLY path that freed that resource, and it had two independent failure modes
(autopilot outages; the sticky manual-unarchive opt-out), so the capacity cap could
fill with finished-but-unarchived sessions and deadlock the queue forever.

`_persist_status` now pops + closes the driver itself on the `complete` transition
(see orchestrator.py, same pop+suppress+aclose idiom as close_session/stop()).
Nothing is archived: the row and transcript stay, and reopening lazily rebuilds the
driver via `ensure_driver` (resume=engine_session_id).

ONLY `complete`. `done` is NOT a finished-work signal — `derive_status` returns it
for any ended turn (turns_completed >= 1, no turn active), i.e. after every ordinary
prompt — so releasing on `done` killed the claude process at the end of every turn in
every session. `done` and `idle` both keep their drivers: those sessions are alive
and awaiting input.

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

    ``engine_session_id``/``last_pr_resolution`` are attributes ``_persist_status``
    and ``transcript_path`` read via ``getattr``/truthiness checks on any live
    driver — None makes both fall through to their detached-session fallback.
    """

    engine_session_id = None
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
                engine_session_id="claude-s1", status="in_progress", cwd="/repo/s1")
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


def test_done_status_keeps_its_driver(tmp_path):
    """`done` is the END OF A TURN, not the end of the session.

    This test asserted the exact opposite while `done` was (wrongly) treated as a
    finished-work signal: it enshrined a release that fired after EVERY ordinary
    turn in EVERY session, killing the claude process the user was about to prompt
    again. A `done` session is live between turns and keeps its warm driver.
    """
    async def run():
        orch = await _build_orch(tmp_path)
        driver = _FakeDriver()
        orch._drivers["s1"] = driver
        await orch._persist_status("s1", "done")
        assert "s1" in orch._drivers
        assert orch._drivers["s1"] is driver
        assert "s1" not in orch._releasing   # no release scheduled at all
        await _drain(orch)
        return orch, driver
    orch, driver = asyncio.run(run())
    assert driver.closed is False
    assert orch._drivers["s1"] is driver


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
# building a replacement driver against the same engine_session_id/cwd — this race
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


def test_release_wait_does_not_hold_the_orchestrator_wide_ensure_lock(tmp_path):
    """#2655: the release wait ran under `_ensure_lock`, so one session's teardown
    froze every WS attach / prompt resume of EVERY other session for up to 20 s.
    It now waits before taking the lock; a concurrent attach elsewhere is untouched."""
    async def run():
        cwd = str(tmp_path / "repo")
        orch = await _build_orch(tmp_path, cwd=cwd)
        orch.record("s1").auto_archive_opt_out_reason = "manual-unarchive"
        driver = _GatedCloseDriver()
        orch._drivers["s1"] = driver
        await orch._persist_status("s1", "complete")
        with patch("server.sessions.orchestrator.compose", return_value=tmp_path / "cfg"), \
             patch("server.sessions.orchestrator.configdir.trust_project"):
            ensure_task = asyncio.create_task(orch.ensure_driver("s1"))
            await asyncio.sleep(0.05)
            assert not ensure_task.done(), "still waiting on the gated release"
            locked = orch._ensure_lock.locked()
            driver.gate.set()
            new_driver = await asyncio.wait_for(ensure_task, timeout=5)
        await _drain(orch)
        return locked, new_driver
    locked, new_driver = asyncio.run(run())
    assert locked is False, "the release wait must not hold _ensure_lock"
    assert new_driver is not None


class _SlowCloseDriver(_FakeDriver):
    """aclose() takes ``delay`` — a clean but slow teardown, like the real driver's
    flush + SDK __aexit__ (≈20 s in prod) against the 20 s release wait."""

    def __init__(self, delay: float):
        super().__init__()
        self.delay = delay

    async def aclose(self):
        await asyncio.sleep(self.delay)
        await super().aclose()


def test_completion_nudge_waits_out_its_own_release_before_resuming(tmp_path, caplog):
    """#2655: the nudge called ensure_driver milliseconds after `_persist_status`
    spawned the release, so ensure_driver's DRIVER_RELEASE_WAIT_S always lapsed on a
    slow-but-healthy aclose() and the replacement resumed the same conversation over
    the old process (the #1012 lost-tail race), logging a WARNING every nudge cycle.
    The nudge now waits for the release itself, with the driver's full close budget."""
    import logging

    verdict = _unmet_verdict()

    async def run():
        cwd = str(tmp_path / "repo")
        orch = await _build_orch(tmp_path, cwd=cwd)
        old = _SlowCloseDriver(delay=0.3)
        orch._drivers["s1"] = old
        closed_at_resume: list[bool] = []
        real_ensure = orch.ensure_driver

        async def spy_ensure(sid, identity="system"):
            closed_at_resume.append(old.closed)
            return await real_ensure(sid, identity)

        orch.ensure_driver = spy_ensure
        with patch("server.sessions.orchestrator.DRIVER_RELEASE_WAIT_S", 0.05), \
             patch("server.automations.completion_bar.met_bar", return_value=verdict), \
             patch("server.sessions.orchestrator.compose", return_value=tmp_path / "cfg"), \
             patch("server.sessions.orchestrator.configdir.trust_project"):
            # Opt out only across the transition, so `_persist_status` does not
            # schedule its own (unawaited) autoarchive task; the nudge is driven
            # directly below, milliseconds after the release spawned — as in prod.
            rec = orch.record("s1")
            rec.auto_archive_opt_out_reason = "manual-unarchive"
            await orch._persist_status("s1", "complete")
            rec.auto_archive_opt_out_reason = None
            assert "s1" in orch._releasing
            await orch._auto_archive_completed("s1")
            await _drain(orch)
        return orch, old, closed_at_resume

    with caplog.at_level(logging.WARNING, logger="server.sessions.orchestrator"):
        orch, old, closed_at_resume = asyncio.run(run())
    assert not [r for r in caplog.records
                if "driver release still in flight" in r.getMessage()]
    assert closed_at_resume and closed_at_resume[0] is True, (
        "the replacement must be built only after the old aclose() returned")
    assert orch._drivers.get("s1") is not None and orch._drivers["s1"] is not old
    assert orch._last_prompt.get("s1") == verdict.remedy


def test_completion_release_budget_covers_the_driver_close_budget():
    """Single source of truth: the nudge's wait derives from aclose()'s own stage
    bounds, so a future close-constant bump cannot slip past it again."""
    from server.sessions import claude_driver
    from server.sessions.orchestrator import COMPLETION_RELEASE_WAIT_S
    assert COMPLETION_RELEASE_WAIT_S > claude_driver.ACLOSE_BUDGET_S
    assert claude_driver.ACLOSE_BUDGET_S >= (claude_driver._TRANSCRIPT_FLUSH_GRACE_S
                                             + claude_driver._CLOSE_AEXIT_TIMEOUT_S)


# --- Review findings (Task 3 follow-up: drain trigger)---------------------------
#
# Task 3 exempted `complete` from the capacity count (CAPACITY_EXEMPT_STATUSES),
# but the drain trigger a few lines below the driver-release block in
# _persist_status still gated on the OLD set (INACTIVE_SESSION_STATUSES, i.e. only
# ended/archived). So a complete transition freed a slot in the count but nothing
# woke the queue for it — it would only drain on some later, unrelated event
# (another close/archive, a rehydrate, a settings change, a manual RPC).
# The trigger now reads CAPACITY_EXEMPT_STATUSES (excluding "queued" — see comment
# at the call site — since a session entering "queued" hasn't freed anything).
# `done` is NOT exempt and frees nothing, so it correctly no longer fires it.


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


def test_done_transition_does_not_schedule_a_queue_drain(tmp_path):
    """The drain trigger is "a slot just freed" (CAPACITY_EXEMPT_STATUSES). A `done`
    transition frees nothing — the session keeps its driver and its slot — so it must
    not wake the queue. (While `done` was exempt this test asserted the opposite.)"""
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
    assert not drained, "a done transition frees no slot — nothing to drain"


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


# --- Final-review findings C4 + I1 ------------------------------------------------


def test_resumed_complete_session_counts_toward_capacity_again(tmp_path):
    """C4: the cap must stay a ceiling.

    `complete` is capacity-exempt precisely because its claude process was released.
    Every resume path (autopilot assist, _rearm_session, restore_deploy /
    _recover_resume_failures_once, a user prompt over the WS) goes through
    ensure_driver, which refuses only `queued` — so a resumed `complete` session used
    to become a live process that never passed the capacity gate AND stayed uncounted
    while it ran. ensure_driver now transitions it back to `done`, which makes the
    existing accounting true again rather than bolting on a second capacity check.
    """
    async def run():
        cwd = str(tmp_path / "repo")
        orch = await _build_orch(tmp_path, status="complete", cwd=cwd)
        orch.record("s1").auto_archive_opt_out_reason = "manual-unarchive"
        assert orch._active_session_count() == 0   # exempt while finished
        with patch("server.sessions.orchestrator.compose", return_value=tmp_path / "cfg"), \
             patch("server.sessions.orchestrator.configdir.trust_project"):
            driver = await orch.ensure_driver("s1")
        assert driver is not None
        async with db_session() as db:
            from sqlalchemy import select as sa_select
            row = (await db.execute(
                sa_select(DbSession).where(DbSession.id == "s1"))).scalars().first()
        return orch, row.status
    orch, db_status = asyncio.run(run())
    assert orch.record("s1").status == "done", "a resumed session is no longer finished"
    assert db_status == "done", "the DB row must agree — rehydrate reads it"
    assert orch._active_session_count() == 1, (
        "a session holding a live driver must consume a capacity slot")


def test_ensure_driver_gives_up_on_a_stuck_release_and_rebuilds(tmp_path):
    """I1: the release wait is bounded.

    ensure_driver waits for an in-flight completion release so it does not race the
    old process's session-file flush — but it does so under the orchestrator-wide
    `_ensure_lock`, and aclose() is only partly bounded internally (the SDK client's
    __aexit__ and the account sync-back are not). Unbounded, one stuck teardown would
    block every attach in the process and make the session permanently unopenable. On
    timeout it must build the replacement anyway.
    """
    async def run():
        cwd = str(tmp_path / "repo")
        orch = await _build_orch(tmp_path, cwd=cwd)
        orch.record("s1").auto_archive_opt_out_reason = "manual-unarchive"
        driver = _GatedCloseDriver()      # aclose() blocks forever; gate never set
        orch._drivers["s1"] = driver
        await orch._persist_status("s1", "complete")
        assert "s1" in orch._releasing

        with patch("server.sessions.orchestrator.DRIVER_RELEASE_WAIT_S", 0.2), \
             patch("server.sessions.orchestrator.compose", return_value=tmp_path / "cfg"), \
             patch("server.sessions.orchestrator.configdir.trust_project"):
            # Unbounded, this never returns. The outer wait_for is the assertion.
            new_driver = await asyncio.wait_for(orch.ensure_driver("s1"), timeout=5)
        assert driver.closed is False     # the old release really is still stuck
        driver.gate.set()                 # clean up the leaked release task
        await _drain(orch)
        return new_driver, driver
    new_driver, old_driver = asyncio.run(run())
    assert new_driver is not None
    assert new_driver is not old_driver


def test_completion_nudge_is_delivered_after_the_driver_was_released(tmp_path):
    """C3: the "goal not met → nudge back to work" prompt must actually arrive.

    `_persist_status` releases the driver on `complete`, and `_auto_archive_completed`
    runs after it — so its `send_prompt` found no driver and returned the "no_driver"
    sentinel. Being a RETURN VALUE and not an exception, even the
    `completion.nudge_failed` log never fired: a premature completion marker parked the
    session at `complete` with the corrective nudge silently thrown away. The nudge path
    now resumes the session first.
    """
    from types import SimpleNamespace

    verdict = SimpleNamespace(met=False, confidence="high", reason="no PR opened",
                              remedy="Open the PR before signalling completion.")

    async def run():
        cwd = str(tmp_path / "repo")
        orch = await _build_orch(tmp_path, status="complete", cwd=cwd)
        assert "s1" not in orch._drivers   # released by completion, as in production
        with patch("server.automations.completion_bar.met_bar", return_value=verdict), \
             patch("server.sessions.orchestrator.compose", return_value=tmp_path / "cfg"), \
             patch("server.sessions.orchestrator.configdir.trust_project"):
            await orch._auto_archive_completed("s1")
        # Drain before asyncio.run() closes the loop. Persisting a status spawns
        # follow-up work (the autopilot nudge among it); left pending, those tasks
        # are destroyed with the loop and the orchestrator survives holding them,
        # which is how this file poisoned later files sharing an xdist worker.
        await _drain(orch)
        return orch
    orch = asyncio.run(run())
    # send_prompt records _last_prompt ONLY on the delivered path — a "no_driver"
    # return leaves it unset, which is exactly the silent drop this covers.
    assert orch._last_prompt.get("s1") == verdict.remedy
    assert "s1" in orch._drivers, "the nudge must run on a live driver"
    assert orch.record("s1").status != "complete", "a nudged session is no longer complete"


def _unmet_verdict():
    from types import SimpleNamespace
    return SimpleNamespace(met=False, confidence="high", reason="no PR opened",
                           remedy="Open the PR before signalling completion.")


async def _nudge_during_drain(tmp_path, orch, verdict):
    with patch("server.automations.completion_bar.met_bar", return_value=verdict), \
         patch("server.sessions.orchestrator.compose", return_value=tmp_path / "cfg"), \
         patch("server.sessions.orchestrator.configdir.trust_project"):
        await orch.begin_drain(selfclear_sec=60)
        await orch._auto_archive_completed("s1")


def test_completion_nudge_refused_by_a_drain_is_not_a_warning(tmp_path, caplog):
    """#2271: a drain refusing the nudge is expected (send_prompt already logs it at
    info) — the nudge call site must not escalate it to a WARNING, which auto-files."""
    import logging
    verdict = _unmet_verdict()

    async def run():
        orch = await _build_orch(tmp_path, status="complete", cwd=str(tmp_path / "repo"))
        with caplog.at_level(logging.INFO, logger="server.sessions.orchestrator"):
            await _nudge_during_drain(tmp_path, orch, verdict)
        orch.cancel_drain()
        await _drain(orch)
    asyncio.run(run())
    warnings = [r for r in caplog.records
                if r.levelno >= logging.WARNING and "nudge_failed" in r.getMessage()]
    assert warnings == []
    assert any("completion.nudge_deferred" in r.getMessage() for r in caplog.records)


def test_completion_nudge_refused_by_a_drain_is_delivered_when_the_drain_cancels(tmp_path):
    """#2271: the nudge is not silently lost — an aborted drain delivers it."""
    verdict = _unmet_verdict()

    async def run():
        orch = await _build_orch(tmp_path, status="complete", cwd=str(tmp_path / "repo"))
        await _nudge_during_drain(tmp_path, orch, verdict)
        assert orch._last_prompt.get("s1") is None   # refused while draining
        orch.cancel_drain()
        await _drain(orch)
        return orch
    orch = asyncio.run(run())
    assert orch._last_prompt.get("s1") == verdict.remedy
    assert orch._drain_deferred_nudges == {}


def test_completion_nudge_refused_by_a_drain_survives_the_restart(tmp_path):
    """#2271: a drain that ends in a restart replays the nudge from ``pending_prompt``
    at boot — and the drain snapshot pass must not clobber it back to None."""
    from sqlalchemy import select as sa_select
    verdict = _unmet_verdict()

    async def run():
        orch = await _build_orch(tmp_path, status="complete", cwd=str(tmp_path / "repo"))
        await _nudge_during_drain(tmp_path, orch, verdict)
        await orch._snapshot_open_tasks()     # a late snapshot pass after the refusal
        async with db_session() as db:
            row = (await db.execute(sa_select(DbSession).where(
                DbSession.id == "s1"))).scalar_one()
        # Leave the drain without cancel_drain's snapshot wipe, as a restart would.
        orch._draining = False
        if orch._drain_timer is not None:
            orch._drain_timer.cancel()
        await _drain(orch)
        return row.pending_prompt
    assert asyncio.run(run()) == verdict.remedy
