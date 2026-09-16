# Session Lease Ownership Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make it impossible for a daemon to build, keep, or lose a session driver for a row it does not own: fence ownership at the one lease choke point, keep the lease alive while a driver is being built, and hand back any lease held for a foreign row.

**Architecture:** All three layers live in `SessionOrchestrator` (`server/sessions/orchestrator.py`) around two existing methods: `_claim_session` (the only path every driver build goes through) and `_session_lease_tick` (the 24-second heartbeat). Ownership policy stays in one place by factoring the stamp rule out of `_owns_record` into `_owns_record_by_stamp`. No new RPC, schema, or settings; the existing `CCSWITCH_SESSION_LEASE_ENFORCE` switch governs all three layers.

**Tech Stack:** Python 3.12, asyncio, SQLAlchemy async (SQLite in tests), pytest + pytest-asyncio, `unittest.mock.AsyncMock`.

**Spec:** `docs/superpowers/specs/2026-09-16-session-lease-ownership-design.md`

## Global Constraints

- No new `CCSWITCH_*` environment variables (brand ratchet ceiling only goes down); no new settings at all in this plan.
- Never use `func.now()`; the lease module already uses `datetime.utcnow()` for its own columns — do not touch it.
- Run tests as `env -i HOME=/tmp/vp-home-$$ PATH="$PATH" PYTHONPATH="$PWD/src" CCSWITCH_DEV=1 <venv>/bin/python -m pytest …` (the real shell env leaks `CCSWITCH_SESSIOND_*` and a live tailnet allowlist). `<venv>` is `/home/tony/GitHub/viberpit-deploy/.venv` on a worktree.
- Before pushing, all four gate steps must be green: `ruff check src/ server/`, `mypy server/ --ignore-missing-imports`, `pytest tests/`, `pytest server/tests/`.
- Commit messages go in a file and are passed with `-F`; end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- Branch off `origin/master` (`git fetch && git checkout -b fix/session-lease-ownership origin/master`), never the local `master` ref.
- Log events use `log_event(log, level, "event.name", **fields)` from `server.obs.context` (already imported in the orchestrator).

---

### Task 1: Factor the stamp-only ownership rule out of `_owns_record`

Layer 4 must ask "do I own this row **by stamp**?" for a session that has a live driver here, and the first clause of `_owns_record` answers "yes, live driver" for exactly that case. Split the stamp rule into its own method so both questions share one source of truth.

**Files:**
- Modify: `server/sessions/orchestrator.py` — `_owns_record` (search `def _owns_record(self, record: SessionRecord,`)
- Test: `server/tests/test_host_partitioned_ownership.py` (existing `_record` helper), new file `server/tests/test_lease_ownership_layers.py`

**Interfaces:**
- Produces: `SessionOrchestrator._owns_record_by_stamp(self, host: str | None, enabled_hosts: frozenset[str] | None) -> bool`. Same truth table as today's `_owns_record` minus the live-driver clause. `_owns_record` keeps its signature and now delegates.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_lease_ownership_layers.py`:

```python
"""Session lease ownership: choke-point fence, build-window grace, self-healing
reconcile (spec docs/superpowers/specs/2026-09-16-session-lease-ownership-design.md).

Companion to test_session_lease.py (the lease protocol and gate) and
test_recovery_actor_ownership.py (per-caller fences). These pin the three layers
that make the 331f7d98 split-brain (#1824, #1826) structurally impossible."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from server.sessions.orchestrator import SessionOrchestrator
from server.tests.test_host_partitioned_ownership import _record

BOTH = frozenset({"dobby", "rocky"})


@pytest.fixture
def primary(monkeypatch):
    import server.sessions.orchestrator as mod
    monkeypatch.setattr(mod, "daemon_role", lambda: "primary")
    monkeypatch.setenv("CCSWITCH_HOST_NAME", "dobby")
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    orch._records = {}
    orch._drivers = {}
    orch._enabled_host_names = AsyncMock(return_value=BOTH)
    return orch


@pytest.mark.parametrize("host,enabled,expected", [
    ("dobby", BOTH, True),        # my stamp
    (None, BOTH, True),           # legacy / primary automation spawn
    ("rocky", BOTH, False),       # another enabled host's row
    ("rocky", None, False),       # registry unknown → fail closed
    ("gone", BOTH, True),         # stamp left the registry → orphan, primary adopts
])
def test_owns_record_by_stamp_is_the_stamp_rule(primary, host, enabled, expected):
    assert primary._owns_record_by_stamp(host, enabled) is expected


def test_owns_record_still_prefers_a_live_driver(primary):
    """The live-driver clause stays in _owns_record and ONLY there."""
    primary._drivers["s1"] = object()
    rec = _record("s1", "rocky", "idle")
    assert primary._owns_record(rec, BOTH) is True
    assert primary._owns_record_by_stamp("rocky", BOTH) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest server/tests/test_lease_ownership_layers.py -v`
Expected: FAIL with `AttributeError: 'SessionOrchestrator' object has no attribute '_owns_record_by_stamp'`

- [ ] **Step 3: Split the method**

Replace the body of `_owns_record` so that everything after the live-driver check moves into the new method:

```python
    def _owns_record(self, record: SessionRecord,
                     enabled_hosts: frozenset[str] | None) -> bool:
        """True iff THIS daemon is the recovery/rotation actor for *record* (#852).
        (docstring unchanged)
        """
        if record.id in getattr(self, "_drivers", {}):
            # The live driver is in this process — de facto ours regardless of
            # what the stamp says (e.g. a mid-flight registry rename).
            return True
        return self._owns_record_by_stamp(record.host, enabled_hosts)

    def _owns_record_by_stamp(self, host: str | None,
                              enabled_hosts: frozenset[str] | None) -> bool:
        """The stamp half of ``_owns_record``: does *host* (a row's fleet stamp)
        belong to this daemon, ignoring whether a driver for it is live here?

        Split out so the lease reconcile (spec layer 4) can ask the question for a
        session whose driver IS live here — the case ``_owns_record``'s first clause
        answers "yes" for by construction. Keep the two in lock-step: this is the
        single source of truth for the stamp policy.
        """
        me = self_host_name()
        if daemon_role() == "worker":
            return host == me
        # Primary owns: unstamped rows (pre-fleet legacy + its own automation
        # spawns, which never pass through the dispatcher), its own stamp, and
        # DEFAULT_HOST_NAME — a UDS registry entry is this host by definition
        # (same rule as the health check's split-brain guard, #844). A row
        # stamped for another ENABLED host has a live daemon responsible for it;
        # a stamp that has left the registry is an orphan the primary adopts.
        from server.sessions.fleet_router import DEFAULT_HOST_NAME
        if host is None or host == me or host == DEFAULT_HOST_NAME:
            return True
        if enabled_hosts is None:  # registry unknown — don't race a live worker
            return False
        return host not in enabled_hosts
```

- [ ] **Step 4: Run the new test and the existing ownership suites**

Run: `pytest server/tests/test_lease_ownership_layers.py server/tests/test_host_partitioned_ownership.py server/tests/test_recovery_actor_ownership.py -v`
Expected: all PASS (the existing truth-table tests prove the split changed nothing).

- [ ] **Step 5: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_lease_ownership_layers.py
printf 'refactor(sessions): split the stamp rule out of _owns_record\n\nLayer 4 of the lease-ownership spec needs "do I own this row by stamp?"\nfor a session whose driver is live here — the case _owns_record answers\n"yes" for by construction. One source of truth for the stamp policy.\n\nRefs #1826\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c1.txt
git commit -F /tmp/c1.txt
```

---

### Task 2: Layer 1 — ownership fence inside `_claim_session`

**Files:**
- Modify: `server/sessions/orchestrator.py` — `_claim_session` (search `async def _claim_session(self, session_id: str, *, why: str)`), `adopt_session` (search `await self._claim_session(session_id, why="adopt")`)
- Test: `server/tests/test_lease_ownership_layers.py`, `server/tests/test_session_lease.py` (end-to-end)

**Interfaces:**
- Produces: `_claim_session(self, session_id: str, *, why: str, adopt: bool = False) -> bool`. Returns `False` without touching the lease row when `self._records[session_id]` exists, `adopt` is false, and `_owns_record` is false for it (unless enforcement is off).
- Consumes: `_owns_record` (Task 1 unchanged signature), `_enabled_host_names()`, `session_lease_enforced()`.

- [ ] **Step 1: Write the failing unit tests**

Append to `server/tests/test_lease_ownership_layers.py`:

```python
import logging

from server.sessions import session_lease


@pytest.fixture
def claiming(primary, monkeypatch):
    """A primary whose lease acquire is recorded instead of hitting the DB, so the
    fence is tested in isolation from the protocol (test_session_lease covers that)."""
    calls: list[str] = []

    async def fake_acquire(session_id, holder, *, ttl_s):
        calls.append(session_id)
        return 1

    monkeypatch.setattr(session_lease, "acquire", fake_acquire)
    primary.acquired = calls
    return primary


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [BOTH, None])
async def test_claim_refuses_a_row_another_host_owns_before_touching_the_lease(
        claiming, caplog, enabled):
    claiming._enabled_host_names.return_value = enabled
    claiming._records["s1"] = _record("s1", "rocky", "idle")
    with caplog.at_level(logging.WARNING, logger="server.sessions.orchestrator"):
        assert await claiming._claim_session("s1", why="resume") is False
    assert claiming.acquired == []                 # never reached acquire()
    assert "s1" not in claiming._lease_fences()
    denied = [r for r in caplog.records if getattr(r, "event", None) == "session.lease_denied"]
    assert denied and denied[0].fields["reason"] == "foreign_stamp:resume"
    assert denied[0].fields["holder"] == "rocky"


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["dobby", None, "gone"])
async def test_claim_proceeds_for_rows_the_primary_owns(claiming, host):
    claiming._records["s1"] = _record("s1", host, "idle")
    assert await claiming._claim_session("s1", why="resume") is True
    assert claiming.acquired == ["s1"]


@pytest.mark.asyncio
async def test_claim_without_a_record_is_decided_by_the_lease_alone(claiming):
    """A fresh spawn has no record yet (D3); the lease protocol decides as before."""
    assert await claiming._claim_session("new", why="spawn") is True
    assert claiming.acquired == ["new"]


@pytest.mark.asyncio
async def test_adopt_bypasses_the_stamp_fence(claiming):
    claiming._records["s1"] = _record("s1", "rocky", "idle")
    assert await claiming._claim_session("s1", why="adopt", adopt=True) is True
    assert claiming.acquired == ["s1"]


@pytest.mark.asyncio
async def test_worker_claims_only_its_own_stamp(claiming, monkeypatch):
    import server.sessions.orchestrator as mod
    monkeypatch.setattr(mod, "daemon_role", lambda: "worker")
    monkeypatch.setenv("CCSWITCH_HOST_NAME", "rocky")
    claiming._records["mine"] = _record("mine", "rocky", "idle")
    claiming._records["primary"] = _record("primary", "dobby", "idle")
    claiming._records["legacy"] = _record("legacy", None, "idle")
    assert await claiming._claim_session("mine", why="resume") is True
    assert await claiming._claim_session("primary", why="resume") is False
    assert await claiming._claim_session("legacy", why="resume") is False
    assert claiming.acquired == ["mine"]


@pytest.mark.asyncio
async def test_enforcement_off_claims_a_foreign_row_and_warns(claiming, monkeypatch, caplog):
    monkeypatch.setenv("CCSWITCH_SESSION_LEASE_ENFORCE", "false")
    claiming._records["s1"] = _record("s1", "rocky", "idle")
    with caplog.at_level(logging.WARNING, logger="server.sessions.orchestrator"):
        assert await claiming._claim_session("s1", why="resume") is True
    assert claiming.acquired == ["s1"]
    assert any("enforcement is OFF" in r.getMessage() for r in caplog.records)
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest server/tests/test_lease_ownership_layers.py -v -k "claim or adopt or worker or enforcement"`
Expected: the refusal tests FAIL (`acquired == ['s1']`, no `session.lease_denied` record); `test_adopt_bypasses_the_stamp_fence` FAILS with `TypeError: ... unexpected keyword argument 'adopt'`.

- [ ] **Step 3: Implement the fence**

Change the signature and insert the fence before the existing `try: fence = await session_lease.acquire(` block:

```python
    async def _claim_session(self, session_id: str, *, why: str,
                             adopt: bool = False) -> bool:
        """Win the right to run a CLI for *session_id* on THIS host, or refuse.

        (existing docstring, then add:)

        Before the lease is consulted at all, the row's fleet stamp is checked
        (spec 2026-09-16-session-lease-ownership, layer 1): a record another
        ENABLED host owns is refused here, whoever asked. The lease answers "is a
        twin live?"; this answers "is it mine to run?" — and every driver build
        goes through this method, so no caller can forget the second question
        (#1824 was a caller that did). ``adopt=True`` is for the paths that take a
        row they do not yet own on purpose (adopt_session, transfer import). A
        session with no in-memory record (a fresh spawn) is decided by the lease
        alone, exactly as before.
        """
        me = self_host_name()
        rec = self._records.get(session_id) if hasattr(self, "_records") else None
        if rec is not None and not adopt:
            getter = getattr(self, "_enabled_host_names", None)
            enabled = await getter() if getter is not None else None
            if not self._owns_record(rec, enabled):
                stamp = getattr(rec, "host", None)
                if session_lease_enforced():
                    log.warning(
                        "session-lease: refusing to %s session %s — its row is stamped "
                        "for host %r, not %r; leaving it to its owner (layer 1).",
                        why, session_id, stamp, me)
                    log_event(log, logging.WARNING, "session.lease_denied",
                              session_id=session_id, holder=stamp,
                              reason=f"foreign_stamp:{why}")
                    return False
                log.warning(
                    "session-lease: session %s is stamped for %r but enforcement is "
                    "OFF — claiming it on %r anyway (%s)", session_id, stamp, me, why)
        try:
            fence = await session_lease.acquire(
                session_id, me, ttl_s=SESSION_LEASE_TTL_S)
        ...  # rest unchanged
```

Then in `adopt_session` change the claim to `await self._claim_session(session_id, why="adopt", adopt=True)`.

- [ ] **Step 4: Run the unit tests**

Run: `pytest server/tests/test_lease_ownership_layers.py -v`
Expected: all PASS.

- [ ] **Step 5: Write the failing end-to-end test**

Append to `server/tests/test_session_lease.py` (it already has `_new_orch`, `_record`, `init_db`, `dispose_db` imports):

```python
@pytest.mark.asyncio
async def test_ensure_driver_never_builds_for_a_row_another_host_owns(tmp_path, monkeypatch):
    """Layer 1 end-to-end (#1824): the primary asks to resume a rocky-stamped row.
    No lease row is written, no SDK process is built, and the cwd is NOT created —
    the empty mkdir'd directory was how the wrong daemon ran a coding agent in a
    non-repo for a day."""
    import server.sessions.orchestrator as mod
    from unittest.mock import AsyncMock
    monkeypatch.setenv("CCSWITCH_HOST_NAME", "dobby")
    monkeypatch.setattr(mod, "daemon_role", lambda: "primary")
    await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path/'c.db'}")
    try:
        orch = _new_orch()
        orch._enabled_host_names = AsyncMock(return_value=frozenset({"dobby", "rocky"}))
        cwd = tmp_path / "wt" / "foreign"
        orch._records[SID] = _record(host="rocky", cwd=str(cwd))

        assert await orch.ensure_driver(SID) is None
        assert SID not in orch._drivers
        assert await session_lease.current(SID) is None      # never touched the lease
        assert not cwd.exists()                               # never mkdir'd the cwd
    finally:
        await dispose_db()
```

- [ ] **Step 6: Run it**

Run: `pytest server/tests/test_session_lease.py::test_ensure_driver_never_builds_for_a_row_another_host_owns -v`
Expected: PASS. This test is written after Step 3 on purpose: its job is to pin that the fence sits **before** `Path(rec.cwd).mkdir(...)` in `ensure_driver` (the `cwd.exists()` assertion), which no unit test in Step 1 covers. To see it fail, run it once with `CCSWITCH_SESSION_LEASE_ENFORCE=false` in the environment: it must FAIL on `not cwd.exists()`. Unset the variable and re-run: PASS.

- [ ] **Step 7: Run the whole lease and adopt suites**

Run: `pytest server/tests/test_session_lease.py server/tests/test_lease_ownership_layers.py server/tests/test_recovery_actor_ownership.py server/tests/test_host_partitioned_ownership.py -v && pytest server/tests/ -q -k adopt`
Expected: all PASS (adopt tests prove the `adopt=True` path still builds).

- [ ] **Step 8: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_lease_ownership_layers.py server/tests/test_session_lease.py
printf 'fix(sessions): fence row ownership at the lease choke point\n\n_claim_session now refuses, before touching the lease row, any record that\n_owns_record says another enabled host owns. Every driver build goes through\nit, so no caller can recreate #1824 by forgetting a per-caller check.\nadopt=True is for adopt_session and the future transfer import. Governed by\nthe existing CCSWITCH_SESSION_LEASE_ENFORCE switch.\n\nRefs #1826\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c2.txt
git commit -F /tmp/c2.txt
```

---

### Task 3: Layer 2 — the lease survives the build window

**Files:**
- Modify: `server/sessions/orchestrator.py` — `_claim_session` (record the build marker), `_release_session_lease` (drop it), `_session_lease_tick` (renew instead of release while building); add `_lease_building` accessor next to `_lease_fences`
- Test: `server/tests/test_lease_ownership_layers.py`, `server/tests/test_session_lease.py` (one existing test updated)

**Interfaces:**
- Produces: `_lease_building(self) -> dict[str, float]` (sid → monotonic claim time, created on demand like `_lease_fences`). Uses `self._clock` when present (tests inject it), else `time.monotonic`.
- Consumes: `SPAWN_WAIT_S` (module constant, default 60 s), `session_lease.renew/release`.

- [ ] **Step 1: Update the existing test that assumed instant release**

In `server/tests/test_session_lease.py::test_the_renew_loop_releases_leases_whose_driver_is_gone`, insert before `await orch._session_lease_tick()`:

```python
        # A claim is a build in flight for up to SPAWN_WAIT_S (layer 2); age
        # "dropped" past that so the tick sees a leak, not a build.
        orch._lease_building()["dropped"] -= SPAWN_WAIT_S + 1
```

and add `SPAWN_WAIT_S` to the `from server.sessions.orchestrator import (...)` line.

- [ ] **Step 2: Write the failing tests**

Append to `server/tests/test_lease_ownership_layers.py`:

```python
from server.sessions.orchestrator import SPAWN_WAIT_S


@pytest.fixture
def ticking(claiming, monkeypatch):
    """A primary whose renew/release are recorded; the clock is injectable."""
    renewed: list[str] = []
    released: list[str] = []

    async def fake_renew(session_id, holder, fence):
        renewed.append(session_id)
        return True

    async def fake_release(session_id, holder, fence):
        released.append(session_id)

    monkeypatch.setattr(session_lease, "renew", fake_renew)
    monkeypatch.setattr(session_lease, "release", fake_release)
    claiming.renewed, claiming.released = renewed, released
    claiming._now = 1000.0
    claiming._clock = lambda: claiming._now
    return claiming


@pytest.mark.asyncio
async def test_a_fresh_claim_is_renewed_not_released_while_the_driver_builds(ticking):
    assert await ticking._claim_session("s1", why="resume") is True
    ticking._now += 1.0                       # the tick at t+1 that killed #1826
    await ticking._session_lease_tick()
    assert ticking.released == []
    assert ticking.renewed == ["s1"]
    assert "s1" in ticking._lease_fences()


@pytest.mark.asyncio
async def test_registration_clears_the_build_marker(ticking):
    await ticking._claim_session("s1", why="resume")
    ticking._drivers["s1"] = object()
    await ticking._session_lease_tick()
    assert "s1" not in ticking._lease_building()
    assert ticking.renewed == ["s1"] and ticking.released == []


@pytest.mark.asyncio
async def test_a_build_that_never_registers_is_released_after_spawn_wait(ticking):
    await ticking._claim_session("s1", why="resume")
    ticking._now += SPAWN_WAIT_S + 1
    await ticking._session_lease_tick()
    assert ticking.released == ["s1"]
    assert "s1" not in ticking._lease_fences()
    assert "s1" not in ticking._lease_building()


@pytest.mark.asyncio
async def test_teardown_drops_the_build_marker_too(ticking):
    await ticking._claim_session("s1", why="resume")
    await ticking._release_session_lease("s1")
    assert "s1" not in ticking._lease_building()
    assert ticking.released == ["s1"]
```

- [ ] **Step 3: Run to verify they fail**

Run: `pytest server/tests/test_lease_ownership_layers.py -v -k "build or registration or teardown_drops"`
Expected: FAIL — `AttributeError: '_lease_building'` on the first, and `released == ['s1']` on the renew test once the accessor exists.

- [ ] **Step 4: Implement**

Add next to `_lease_fences`:

```python
    def _lease_building(self) -> dict[str, float]:
        """sid → monotonic time of its most recent lease claim (layer 2).

        A claim happens BEFORE the driver is built and registered, and the build
        takes seconds to tens of seconds. Without this marker the heartbeat tick
        read "fence, no driver" as a leaked lease and released it mid-build (#1826);
        the session then ran with no lease at all. Created on demand for the same
        reason as ``_lease_fences``.
        """
        building = getattr(self, "_session_lease_building", None)
        if building is None:
            building = {}
            self._session_lease_building = building
        return building

    def _lease_clock(self) -> float:
        clock = getattr(self, "_clock", None)
        return clock() if clock is not None else time.monotonic()
```

In `_claim_session`, right after `self._lease_fences()[session_id] = fence`:

```python
            self._lease_building()[session_id] = self._lease_clock()
```

In `_release_session_lease`, first line of the body:

```python
        self._lease_building().pop(session_id, None)
```

Replace the loop body of `_session_lease_tick`:

```python
        me = self_host_name()
        now = self._lease_clock()
        for sid, fence in list(self._lease_fences().items()):
            if sid in self._drivers:
                self._lease_building().pop(sid, None)
            elif now - self._lease_building().get(sid, float("-inf")) < SPAWN_WAIT_S:
                # Claimed, driver not registered yet: a build in flight, not a
                # leak. Renew so a build longer than the TTL is never seen as
                # lapsed by another host (layer 2).
                pass
            else:
                await self._release_session_lease(sid)
                continue
            try:
                ok = await session_lease.renew(sid, me, fence)
            except Exception:
                log.debug("session-lease: renew of %s failed (DB error) — "
                          "retrying next beat", sid, exc_info=True)
                continue
            if ok:
                continue
            self._lease_fences().pop(sid, None)
            self._lease_building().pop(sid, None)
            state = await _lease_state(sid)
            log.critical(
                "session-lease: LOST session %s (fenced takeover by %r) while a "
                "driver for it is still live here — TWO hosts may now be running "
                "this conversation (#1379).",
                sid, state.holder if state is not None else "<released>")
```

Note `self._clock` defaults to `time.time` in `__init__`; wall time is monotonic enough for a 60-second grace and tests inject a fake. `time` is already imported.

- [ ] **Step 5: Run the lease suites**

Run: `pytest server/tests/test_lease_ownership_layers.py server/tests/test_session_lease.py -v`
Expected: all PASS, including the updated `test_the_renew_loop_releases_leases_whose_driver_is_gone`.

- [ ] **Step 6: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_lease_ownership_layers.py server/tests/test_session_lease.py
printf 'fix(sessions): keep a session lease alive while its driver is built\n\nThe heartbeat tick released any fence with no registered driver, which\nincluded every build in flight; a build that straddled a tick ran with no\nlease from then on. A claim now marks the session as building for up to\nSPAWN_WAIT_S and the tick renews it during that window instead.\n\nFixes #1826\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c3.txt
git commit -F /tmp/c3.txt
```

---

### Task 4: Layer 4 — self-healing reconcile in the tick

**Files:**
- Modify: `server/sessions/orchestrator.py` — `_session_lease_tick` (add the foreign-holder check for live drivers), new helper `_reconcile_foreign_lease`
- Test: `server/tests/test_lease_ownership_layers.py`

**Interfaces:**
- Produces: `_reconcile_foreign_lease(self, sid: str, rec, enabled: frozenset[str] | None) -> bool` — returns `True` when it tore the driver down (the tick must then skip renew). Pure decision + `_teardown_driver_state` + deferred `_close_automation_driver`.
- Consumes: `_owns_record_by_stamp` (Task 1), `_teardown_driver_state` (existing; pops the driver and releases the lease synchronously), `_close_automation_driver(driver, sid)` (existing), `self._spawn(coro, name=…)` (existing task registry), `session_lease_enforced()`.

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_lease_ownership_layers.py`:

```python
import asyncio


class LiveDriver:
    def __init__(self, turn_active=False, drain_alive=False):
        self.turn_active = turn_active
        self.drain_alive = drain_alive
        self.closed = False

    async def aclose(self):
        self.closed = True


@pytest.fixture
def reconciling(ticking, monkeypatch):
    """`ticking` plus the teardown seams the reconcile calls."""
    torn: list[str] = []

    async def fake_teardown(sid):
        torn.append(sid)
        ticking._lease_fences().pop(sid, None)
        ticking._lease_building().pop(sid, None)
        return ticking._drivers.pop(sid, None)

    async def fake_close(driver, sid):
        driver.closed = True

    ticking._teardown_driver_state = fake_teardown
    ticking._close_automation_driver = fake_close
    ticking._spawn = lambda coro, name=None: asyncio.ensure_future(coro)
    ticking.torn = torn
    return ticking


@pytest.mark.asyncio
async def test_tick_hands_back_a_lease_held_for_a_row_another_host_owns(reconciling, caplog):
    """The automated form of the 2026-09-16 manual repair: dobby holds and drives a
    rocky-stamped row; one tick closes the driver and releases the lease."""
    await reconciling._claim_session("s1", why="adopt", adopt=True)
    reconciling._records["s1"] = _record("s1", "rocky", "idle")
    driver = LiveDriver()
    reconciling._drivers["s1"] = driver
    with caplog.at_level(logging.WARNING, logger="server.sessions.orchestrator"):
        await reconciling._session_lease_tick()
    await asyncio.sleep(0)                        # let the deferred close run
    assert reconciling.torn == ["s1"]
    assert "s1" not in reconciling._drivers and driver.closed
    assert reconciling.renewed == []              # released, not renewed, this pass
    assert "s1" in reconciling._records           # the fleet mirror survives
    ev = [r for r in caplog.records if getattr(r, "event", None) == "session.lease_reconciled"]
    assert ev and ev[0].fields["session_id"] == "s1" and ev[0].fields["host"] == "rocky"


@pytest.mark.asyncio
async def test_tick_defers_while_a_turn_is_active(reconciling, caplog):
    await reconciling._claim_session("s1", why="adopt", adopt=True)
    reconciling._records["s1"] = _record("s1", "rocky", "idle")
    reconciling._drivers["s1"] = LiveDriver(turn_active=True)
    with caplog.at_level(logging.CRITICAL, logger="server.sessions.orchestrator"):
        await reconciling._session_lease_tick()
    assert reconciling.torn == [] and reconciling.renewed == ["s1"]
    ev = [r for r in caplog.records if getattr(r, "event", None) == "session.lease_foreign_holder"]
    assert ev and ev[0].fields["action"] == "deferred_turn_active"


@pytest.mark.asyncio
async def test_tick_never_reconciles_when_the_registry_is_unknown(reconciling):
    reconciling._enabled_host_names.return_value = None
    await reconciling._claim_session("s1", why="adopt", adopt=True)
    reconciling._records["s1"] = _record("s1", "rocky", "idle")
    reconciling._drivers["s1"] = LiveDriver()
    await reconciling._session_lease_tick()
    assert reconciling.torn == [] and reconciling.renewed == ["s1"]


@pytest.mark.asyncio
async def test_tick_logs_only_when_enforcement_is_off(reconciling, monkeypatch, caplog):
    monkeypatch.setenv("CCSWITCH_SESSION_LEASE_ENFORCE", "false")
    await reconciling._claim_session("s1", why="adopt", adopt=True)
    reconciling._records["s1"] = _record("s1", "rocky", "idle")
    reconciling._drivers["s1"] = LiveDriver()
    with caplog.at_level(logging.CRITICAL, logger="server.sessions.orchestrator"):
        await reconciling._session_lease_tick()
    assert reconciling.torn == [] and reconciling.renewed == ["s1"]
    ev = [r for r in caplog.records if getattr(r, "event", None) == "session.lease_foreign_holder"]
    assert ev and ev[0].fields["action"] == "log_only"


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["dobby", None, "gone"])
async def test_tick_leaves_owned_rows_alone(reconciling, host):
    await reconciling._claim_session("s1", why="spawn")
    reconciling._records["s1"] = _record("s1", host, "idle")
    reconciling._drivers["s1"] = LiveDriver()
    await reconciling._session_lease_tick()
    assert reconciling.torn == [] and reconciling.renewed == ["s1"]


@pytest.mark.asyncio
async def test_tick_ignores_a_live_driver_with_no_record(reconciling):
    """test_session_lease's harness registers bare object() drivers with no
    record; the reconcile must not touch them."""
    await reconciling._claim_session("s1", why="spawn")
    reconciling._drivers["s1"] = object()
    await reconciling._session_lease_tick()
    assert reconciling.torn == [] and reconciling.renewed == ["s1"]
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest server/tests/test_lease_ownership_layers.py -v -k "tick_"`
Expected: the hands-back test FAILS (`torn == []`, `renewed == ['s1']`); the deferred/log-only tests FAIL on the missing `session.lease_foreign_holder` event; the leave-alone tests PASS already.

- [ ] **Step 3: Implement**

Add the helper below `_session_lease_tick`:

```python
    async def _reconcile_foreign_lease(self, sid: str, rec: SessionRecord,
                                       enabled: frozenset[str] | None) -> bool:
        """Layer 4: this daemon holds the lease AND a live driver for *sid*, but the
        row is stamped for another enabled host. Give it back.

        Once the wrong daemon holds a session the state is stable — it renews
        forever, the owner is refused forever, the dashboard reads "idle, detached"
        (#1824 lasted a day). Returns True when the driver was torn down, so the
        caller skips this pass's renew. Never acts on an unknown registry (a blip
        must not drop this host's own drivers), never mid-turn (killing a driver
        mid-turn has its own failure modes), and only logs when enforcement is off.
        """
        if enabled is None or self._owns_record_by_stamp(getattr(rec, "host", None), enabled):
            return False
        driver = self._drivers.get(sid)
        if driver is None:
            return False
        stamp = getattr(rec, "host", None)
        if getattr(driver, "turn_active", False) or getattr(driver, "drain_alive", False):
            log_event(log, logging.CRITICAL, "session.lease_foreign_holder",
                      session_id=sid, host=stamp, action="deferred_turn_active")
            return False
        if not session_lease_enforced():
            log_event(log, logging.CRITICAL, "session.lease_foreign_holder",
                      session_id=sid, host=stamp, action="log_only")
            return False
        popped = await self._teardown_driver_state(sid)   # pops driver, releases lease
        if popped is not None:
            self._spawn(self._close_automation_driver(popped, sid),
                        name=f"lease-reconcile-close-{sid}")
        log_event(log, logging.WARNING, "session.lease_reconciled",
                  session_id=sid, host=stamp)
        return True
```

In `_session_lease_tick`, read the registry once and call the helper for live drivers. The loop head becomes:

```python
        me = self_host_name()
        now = self._lease_clock()
        getter = getattr(self, "_enabled_host_names", None)
        enabled = await getter() if getter is not None else None
        for sid, fence in list(self._lease_fences().items()):
            if sid in self._drivers:
                self._lease_building().pop(sid, None)
                rec = self._records.get(sid) if hasattr(self, "_records") else None
                if rec is not None and await self._reconcile_foreign_lease(sid, rec, enabled):
                    continue
            elif now - self._lease_building().get(sid, float("-inf")) < SPAWN_WAIT_S:
                pass
            else:
                await self._release_session_lease(sid)
                continue
            ...  # renew block unchanged from Task 3
```

- [ ] **Step 4: Run all lease suites**

Run: `pytest server/tests/test_lease_ownership_layers.py server/tests/test_session_lease.py server/tests/test_recovery_actor_ownership.py server/tests/test_host_partitioned_ownership.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_lease_ownership_layers.py
printf 'fix(sessions): hand back a session lease held for a row another host owns\n\nThe heartbeat tick now closes a live driver whose row is stamped for another\nenabled host and releases its lease — the automated form of the 2026-09-16\nmanual repair. Deferred while a turn is active, skipped when the registry is\nunknown, log-only when enforcement is off.\n\nRefs #1824\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c4.txt
git commit -F /tmp/c4.txt
```

---

### Task 5: Fix-pattern note, full gate, PR

**Files:**
- Modify: `docs/superpowers/fix-patterns/2026-09-15-fleet-mirrors-are-not-local-actors.md` (the "Where to look" list)

- [ ] **Step 1: Record the structural fix in the fix-pattern note**

Append to the "Where to look" bullet list:

```markdown
- Structural fix (spec 2026-09-16-session-lease-ownership): `_claim_session` now applies
  `_owns_record` itself (`adopt=True` bypasses for adopt/transfer), the heartbeat tick
  renews a lease whose driver is still building (`SPAWN_WAIT_S` grace, #1826), and
  `_reconcile_foreign_lease` closes a live driver held for a row another enabled host
  owns. Per-caller fences remain as the cheap first line; the choke point is the backstop.
```

- [ ] **Step 2: Run the four gate steps**

```bash
V=/home/tony/GitHub/viberpit-deploy/.venv/bin/python
$V -m ruff check src/ server/
$V -m mypy server/ --ignore-missing-imports
env -i HOME=/tmp/vp-home-$$ PATH="$PATH" PYTHONPATH="$PWD/src" CCSWITCH_DEV=1 $V -m pytest tests/ -q
env -i HOME=/tmp/vp-home-$$ PATH="$PATH" PYTHONPATH="$PWD/src" CCSWITCH_DEV=1 $V -m pytest server/tests/ -n auto --dist loadfile --timeout=300 -q 2>&1 | tee /tmp/lease-ownership-full.log | tail -5
```

Expected: ruff "All checks passed!", mypy "Success", core suite green, server suite green except the known pre-existing local failures (`test_backlog_query_queue.py::test_gave_up_row_is_a_cooldown_not_a_claim` fails on untouched master in this environment; re-run any other failure alone before believing it). Acquire a test slot via `/api/test-slots` before the full server run on rocky (see `.agents/instructions/core.md`).

- [ ] **Step 3: Commit, push, open the PR, arm auto-merge**

```bash
git add docs/superpowers/fix-patterns/2026-09-15-fleet-mirrors-are-not-local-actors.md
printf 'docs(fix-patterns): record the lease-ownership choke point\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c5.txt
git commit -F /tmp/c5.txt
git push -u origin fix/session-lease-ownership
```

Write `/tmp/pr-lease.md` with exactly this body (fill in the two counts from Step 2):

```markdown
## Why

After the `331f7d98` split-brain (#1824) three gaps remained: ownership was checked by callers, not at the lease choke point; the heartbeat tick released a lease whose driver was still spawning (#1826); and a lease held for a row another host owns was never noticed. Spec: `docs/superpowers/specs/2026-09-16-session-lease-ownership-design.md`.

## What

- `_owns_record_by_stamp`: the stamp rule factored out of `_owns_record` (one source of truth).
- Layer 1: `_claim_session(…, adopt=False)` refuses a record another enabled host owns before touching the lease row; `adopt_session` passes `adopt=True`.
- Layer 2: a claim marks the session as building for `SPAWN_WAIT_S`; the tick renews instead of releasing during that window.
- Layer 4: `_reconcile_foreign_lease` closes a live driver held for a foreign-owned row and releases the lease; deferred mid-turn, skipped on an unknown registry, log-only when `CCSWITCH_SESSION_LEASE_ENFORCE=false`.

No new RPC, schema, or settings.

## Tests

`server/tests/test_lease_ownership_layers.py` (new, N tests), `test_session_lease.py` (+1 end-to-end, 1 updated for the build grace). `ruff`, `mypy`, `pytest tests/`, `pytest server/tests/` (M passed; known local-only backlog failure re-run and confirmed pre-existing).

Fixes #1826
Refs #1824

🤖 Generated with [Claude Code](https://claude.com/claude-code)
```

Then:

```bash
gh pr create --title "fix(sessions): fence, keep, and reconcile session leases by row ownership" --body-file /tmp/pr-lease.md
gh pr merge <#> --squash --auto
```

- [ ] **Step 4: Post-deploy verification (after the deploy controller rolls it out)**

On both hosts, over one normal day:

```bash
journalctl -u viberpit-daemon.service --since "1 day ago" --no-pager | grep -c 'foreign_stamp'     # expect 0 for own rows
journalctl -u viberpit-daemon.service --since "1 day ago" --no-pager | grep 'session.lease_reconciled'
```

And against the prod DB: every `daemon_leases` row named `session:<id>` whose session has a live driver must show `renewed_at` within the last `SESSION_LEASE_TTL_S/5` seconds (the 1970 sentinel on a live session is the #1826 signature and must no longer appear).
