# Session-resilience hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make sessions self-heal from subprocess crashes and wedged turns, and close the remote-mode (daemon) coverage gaps the cutover exposed.

**Architecture:** Reuse the existing failure→recovery machinery (`failure.classify` → `SessionDriver._maybe_signal_recovery` → `orchestrator._handle_recoverable_failure` → `_recover_session`/`_reassign`). A hard crash becomes a new recoverable kind that rebuilds the driver in place (resume from `claude_session_id`), guarded by a per-session cap mirroring the rotation cap. A periodic stall watchdog interrupts turns making no progress. `RemoteDriver` gains the driver methods ws.py calls, and `DaemonError` maps to a clean HTTP response.

**Tech Stack:** Python 3.12, asyncio, FastAPI, pytest (async). The daemon runs the in-process `SessionOrchestrator`; recovery logic lives there and the edge sees results over the existing event stream.

## Global Constraints

- TDD: failing test first, minimal implementation, never skip the red step.
- Single source of truth: all caps/cooldowns live in `server/sessions/recovery.py:RecoveryState` next to `rotation_cap_reached`/`quarantine`; do NOT add parallel retry bookkeeping elsewhere.
- Injected clock: recovery/watchdog timing uses an injected `now`/`clock` (never bare `time.time()` in logic under test).
- New constants defined once in `recovery.py`, env-overridable.
- Run tests against the prebuilt env: `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest …` (a fresh worktree has no built env). Unset `CCSWITCH_ENABLE_XVFB` for the suite to match CI.
- Add new SQLAlchemy columns nullable only (none are required by this plan).

---

## Gap 1 — Transparent crash recovery

### Task 1.1: Classify hard subprocess crashes as `process_crash`

**Files:**
- Modify: `server/sessions/failure.py`
- Test: `server/tests/test_failure_classify.py`

**Interfaces:**
- Produces: `failure.classify(code, message, *, retry_after=None) -> TurnFailure` now returns `kind="process_crash"` (recoverable) for subprocess-death signatures. `TurnFailure.is_recoverable` is True for `process_crash`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_failure_classify.py  (append)
from server.sessions.failure import classify

def test_process_crash_signatures_are_recoverable():
    for msg in (
        "ProcessError: Command failed with exit code -6",
        "ProcessError: Command failed with exit code -4 (exit code: -4)",
        "CLIConnectionError: Failed to start Claude Code: [Errno 11] Resource temporarily unavailable",
        "CLIConnectionError: Cannot write to terminated process (exit code: -6)",
    ):
        f = classify("driver_error", msg)
        assert f.kind == "process_crash", msg
        assert f.is_recoverable is True

def test_ordinary_driver_error_stays_other():
    f = classify("driver_error", "ValueError: bad thing happened")
    assert f.kind == "other"
    assert f.is_recoverable is False

def test_positive_exit_code_is_not_a_crash():
    # a clean non-signal exit is not a hard crash we auto-respawn
    f = classify("driver_error", "ProcessError: Command failed with exit code 1")
    assert f.kind == "other"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_failure_classify.py -k "process_crash or positive_exit or ordinary_driver" -v`
Expected: FAIL (`process_crash` not produced; currently classifies as `other`).

- [ ] **Step 3: Write minimal implementation**

```python
# server/sessions/failure.py
# add to the recoverable set:
_RECOVERABLE = frozenset({"rate_limit", "auth_401", "process_crash"})

# add a detector (module level):
import re  # already imported

def _is_process_crash(text: str) -> bool:
    low = text.lower()
    if "resource temporarily unavailable" in low or "errno 11" in low:
        return True
    if "terminated process" in low or "failed to start claude" in low:
        return True
    # negative/signal exit codes (-2..-N) = killed by signal (SIGABRT/SIGTRAP/…)
    if re.search(r"exit code:?\s*-\d+", low):
        return True
    return False

# in classify(), AFTER the api_429/api_401 explicit-code checks and BEFORE
# the is_429/is_401 content inference, add:
    if _is_process_crash(text):
        return TurnFailure(kind="process_crash")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_failure_classify.py -v`
Expected: PASS (all, including pre-existing).

- [ ] **Step 5: Commit**

```bash
git add server/sessions/failure.py server/tests/test_failure_classify.py
git commit -m "feat(sessions): classify hard subprocess crashes as recoverable process_crash"
```

### Task 1.2: Crash cap in `RecoveryState`

**Files:**
- Modify: `server/sessions/recovery.py`
- Test: `server/tests/test_recovery_state.py`

**Interfaces:**
- Produces: `RecoveryState.record_crash(session_id, *, now)` and `RecoveryState.crash_cap_reached(session_id, *, now) -> bool`; module constants `CRASH_CAP: int = 3`, `CRASH_WINDOW: float = 120.0`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_recovery_state.py  (append)
def test_crash_cap_reached_after_max_within_window():
    s = RecoveryState()
    for t in (0.0, 10.0, 20.0):
        assert not s.crash_cap_reached("sess", now=t)
        s.record_crash("sess", now=t)
    assert s.crash_cap_reached("sess", now=30.0)

def test_crash_cap_resets_outside_window():
    s = RecoveryState()
    s.record_crash("sess", now=0.0)
    s.record_crash("sess", now=10.0)
    s.record_crash("sess", now=20.0)
    assert s.crash_cap_reached("sess", now=30.0)
    assert not s.crash_cap_reached("sess", now=200.0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_recovery_state.py -k crash -v`
Expected: FAIL (`AttributeError: 'RecoveryState' object has no attribute 'record_crash'`).

- [ ] **Step 3: Write minimal implementation**

```python
# server/sessions/recovery.py
# add constants near the others:
CRASH_CAP: int = 3
CRASH_WINDOW: float = 120.0

# in RecoveryState.__init__ add:
        self._crashes: dict[str, list[float]] = defaultdict(list)
        self._crash_cap = CRASH_CAP
        self._crash_window = CRASH_WINDOW

# add methods:
    def record_crash(self, session_id: str, *, now: float) -> None:
        self._crashes[session_id].append(now)

    def crash_cap_reached(self, session_id: str, *, now: float) -> bool:
        cutoff = now - self._crash_window
        recent = [t for t in self._crashes.get(session_id, ()) if t > cutoff]
        self._crashes[session_id] = recent
        return len(recent) >= self._crash_cap
```

- [ ] **Step 4: Run test to verify it passes**

Run: `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_recovery_state.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/sessions/recovery.py server/tests/test_recovery_state.py
git commit -m "feat(recovery): per-session crash cap mirroring rotation cap"
```

### Task 1.3: Orchestrator respawns on `process_crash`, guarded

**Files:**
- Modify: `server/sessions/orchestrator.py` (the `_recover_session` method, ~line 1160)
- Test: `server/tests/test_orchestrator_recovery.py`

**Interfaces:**
- Consumes: `failure` kind `process_crash` (Task 1.1); `RecoveryState.record_crash`/`crash_cap_reached` (Task 1.2); existing `_reassign(record, new_account, *, reason)` (rebuilds driver in place, resumes, re-sends last prompt) and `_pause_session(record, *, reason)`.
- Produces: a `process_crash` branch in `_recover_session`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_orchestrator_recovery.py  (append; mirror existing fixtures in this file)
import pytest

@pytest.mark.asyncio
async def test_process_crash_respawns_driver(make_orchestrator_with_live_session):
    # fixture builds an orchestrator with one live session whose driver can be
    # replaced; see existing rate_limit recovery tests in this file for the helper.
    orch, record = make_orchestrator_with_live_session()
    orig = orch._drivers[record.id]
    await orch._recover_session(record, kind="process_crash", retry_after=None)
    # a fresh driver replaced the crashed one (resumed in place)
    assert orch._drivers[record.id] is not orig

@pytest.mark.asyncio
async def test_process_crash_guard_pauses_after_cap(make_orchestrator_with_live_session):
    orch, record = make_orchestrator_with_live_session()
    now = orch._clock()
    for _ in range(3):
        orch._recovery.record_crash(record.id, now=now)
    paused = []
    orch._pause_session = lambda rec, *, reason: paused.append(reason) or _async_none()
    await orch._recover_session(record, kind="process_crash", retry_after=None)
    assert paused == ["crash_cap"]

def _async_none():
    async def _n(): return None
    return _n()
```

> Note: if `make_orchestrator_with_live_session` does not exist, factor the setup
> used by this file's existing `rate_limit`/`auth_401` recovery tests into a fixture
> with that name as Step 1a, then proceed. Do not invent a new SDK fake — reuse the
> one those tests already use.

- [ ] **Step 2: Run test to verify it fails**

Run: `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_orchestrator_recovery.py -k process_crash -v`
Expected: FAIL (no `process_crash` branch — driver not replaced / guard not applied).

- [ ] **Step 3: Write minimal implementation**

```python
# server/sessions/orchestrator.py  — inside _recover_session, after the rotation_cap
# guard and before the rate_limit branch:
        if kind == "process_crash":
            if self._recovery.crash_cap_reached(record.id, now=now):
                await self._pause_session(record, reason="crash_cap")
                return
            self._recovery.record_crash(record.id, now=now)
            # Rebuild the driver in place on the same account, resuming the
            # conversation; _reassign re-sends the last prompt so the interrupted
            # turn continues. Same machinery rate-limit recovery uses.
            await self._reassign(record, record.account, reason="process_crash")
            return
```

- [ ] **Step 4: Run test to verify it passes**

Run: `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_orchestrator_recovery.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_orchestrator_recovery.py
git commit -m "feat(sessions): auto-respawn driver on process_crash, guarded by crash cap"
```

### Task 1.4: End-to-end driver-crash → recovery signal

**Files:**
- Test: `server/tests/test_driver_recovery.py`

**Interfaces:**
- Consumes: existing `_drain` fallback already calls `_maybe_signal_recovery("driver_error", "<Type>: <detail>")`; with Task 1.1 a crash message routes `on_recoverable_failure(session_id, "process_crash", None)`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_driver_recovery.py  (append; reuse this file's fake SDK client)
import pytest

@pytest.mark.asyncio
async def test_subprocess_crash_signals_process_crash(make_driver_with_crashing_client):
    # fake client whose receive_response raises ProcessError("...exit code -6")
    seen = []
    driver = make_driver_with_crashing_client(
        on_recoverable_failure=lambda sid, kind, ra: seen.append((kind, ra)) or _none())
    await driver.start()
    await driver.send_prompt("go")
    await driver._pump  # let the turn drain and fail
    assert ("process_crash", None) in seen

def _none():
    async def _n(): return None
    return _n()
```

> Reuse the crashing-client fake from this file's existing recovery tests if present;
> otherwise add a minimal fake whose `receive_response` raises
> `claude_agent_sdk._errors.ProcessError("Command failed with exit code -6", exit_code=-6)`.

- [ ] **Step 2: Run test to verify it fails**

Run: `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_driver_recovery.py -k subprocess_crash -v`
Expected: FAIL before Task 1.1 lands; with 1.1 it PASSES (this task is the integration guard — if it already passes, keep it as a regression test).

- [ ] **Step 3: (no new impl — wiring verified by 1.1)**

- [ ] **Step 4: Run the full recovery suite**

Run: `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_driver_recovery.py server/tests/test_orchestrator_recovery.py server/tests/test_failure_classify.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/tests/test_driver_recovery.py
git commit -m "test(sessions): end-to-end subprocess crash signals process_crash recovery"
```

---

## Gap 4 — Stall watchdog for wedged turns

### Task 4.1: Track last-meaningful-progress on the driver

**Files:**
- Modify: `server/sessions/driver.py` (`__init__`, `_drain`)
- Test: `server/tests/test_driver_status.py`

**Interfaces:**
- Produces: `SessionDriver.last_progress_at -> float` (epoch secs of the last meaningful event); updated on non-empty `TextDelta`/`ThinkingDelta`, any `ToolCall`/`ToolResult`, `TurnEnd`, `Question`, `PermissionRequest`. Empty-text deltas do NOT update it.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_driver_status.py  (append)
import pytest
import server.sessions.events as ev

@pytest.mark.asyncio
async def test_last_progress_advances_only_on_meaningful_events(make_driver):
    driver = make_driver(clock=_FakeClock([100.0, 101.0, 102.0, 103.0]).tick)
    base = driver.last_progress_at
    driver._note_progress(ev.ThinkingDelta(turn_id="t", text=""))   # empty → no advance
    assert driver.last_progress_at == base
    driver._note_progress(ev.TextDelta(turn_id="t", text="hello")) # real → advance
    assert driver.last_progress_at > base

class _FakeClock:
    def __init__(self, ticks): self._t = list(ticks)
    def tick(self): return self._t.pop(0) if self._t else 999.0
```

> If `make_driver` isn't already a fixture, reuse the driver builder from
> `test_driver.py`. `_note_progress` is the helper added in Step 3.

- [ ] **Step 2: Run test to verify it fails**

Run: `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_driver_status.py -k last_progress -v`
Expected: FAIL (`_note_progress`/`last_progress_at` missing).

- [ ] **Step 3: Write minimal implementation**

```python
# server/sessions/driver.py
# in __init__:
        self._last_progress_at: float = self._clock()

    @property
    def last_progress_at(self) -> float:
        return self._last_progress_at

    def _note_progress(self, e) -> None:
        import server.sessions.events as ev
        meaningful = (
            (isinstance(e, (ev.TextDelta, ev.ThinkingDelta)) and bool(getattr(e, "text", "")))
            or isinstance(e, (ev.ToolCall, ev.ToolResult, ev.TurnEnd, ev.Question, ev.PermissionRequest))
        )
        if meaningful:
            self._last_progress_at = self._clock()

# in _drain(), in the per-event loop right before `await self._out.put(e)`:
                    self._note_progress(e)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_driver_status.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/sessions/driver.py server/tests/test_driver_status.py
git commit -m "feat(driver): track last meaningful-progress timestamp (ignore empty deltas)"
```

### Task 4.2: Stall watchdog interrupts wedged turns

**Files:**
- Modify: `server/sessions/orchestrator.py` (watchdog task + start/stop hooks)
- Modify: `server/sessions/recovery.py` (`STALL_TIMEOUT` constant, env override)
- Test: `server/tests/test_orchestrator_recovery.py`

**Interfaces:**
- Consumes: `SessionDriver.last_progress_at`, `_turn_active`, `current_status()`; `driver.interrupt()`.
- Produces: `orchestrator._check_stalls(now: float)` (pure step, testable); constant `STALL_TIMEOUT` (default 300.0, env `CCSWITCH_STALL_TIMEOUT_SEC`). A periodic task calls `_check_stalls` and is started in `start()`/cancelled in shutdown.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_orchestrator_recovery.py  (append)
@pytest.mark.asyncio
async def test_check_stalls_interrupts_wedged_turn(make_orchestrator_with_live_session):
    orch, record = make_orchestrator_with_live_session()
    d = orch._drivers[record.id]
    d._turn_active = True
    d._last_progress_at = 0.0          # last progress long ago
    interrupted = []
    d.interrupt = lambda: interrupted.append(True) or _async_none()
    await orch._check_stalls(now=10_000.0)   # >> STALL_TIMEOUT
    assert interrupted == [True]

@pytest.mark.asyncio
async def test_check_stalls_leaves_progressing_turn_alone(make_orchestrator_with_live_session):
    orch, record = make_orchestrator_with_live_session()
    d = orch._drivers[record.id]
    d._turn_active = True
    d._last_progress_at = 9_990.0
    interrupted = []
    d.interrupt = lambda: interrupted.append(True) or _async_none()
    await orch._check_stalls(now=10_000.0)   # within STALL_TIMEOUT
    assert interrupted == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_orchestrator_recovery.py -k check_stalls -v`
Expected: FAIL (`_check_stalls` missing).

- [ ] **Step 3: Write minimal implementation**

```python
# server/sessions/recovery.py
import os
STALL_TIMEOUT: float = float(os.environ.get("CCSWITCH_STALL_TIMEOUT_SEC", "300"))

# server/sessions/orchestrator.py
from server.sessions.recovery import STALL_TIMEOUT

    async def _check_stalls(self, now: float) -> None:
        for sid, driver in list(self._drivers.items()):
            if not getattr(driver, "_turn_active", False):
                continue
            if now - driver.last_progress_at <= STALL_TIMEOUT:
                continue
            if self._recovery.crash_cap_reached(sid, now=now):
                continue  # repeatedly stalling — don't interrupt-loop
            self._recovery.record_crash(sid, now=now)
            log.warning("stall watchdog: interrupting wedged turn session=%s", sid)
            await driver.interrupt()

    async def _stall_loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            try:
                await self._check_stalls(self._clock())
            except Exception:
                log.exception("stall watchdog tick failed")

# in start(): launch and retain the task
        self._stall_task = asyncio.create_task(self._stall_loop(), name="stall-watchdog")
# in the shutdown/stop path: cancel self._stall_task like other background tasks
```

- [ ] **Step 4: Run test to verify it passes**

Run: `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_orchestrator_recovery.py -k check_stalls -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/sessions/orchestrator.py server/sessions/recovery.py server/tests/test_orchestrator_recovery.py
git commit -m "feat(sessions): stall watchdog interrupts turns wedged with no progress"
```

---

## Gap 2 — RemoteDriver method parity

### Task 2.1: `seed_idle_suggestions` daemon RPC + RemoteDriver forwarding

**Files:**
- Modify: `server/ipc/orchestrator_rpc.py` (register RPC)
- Modify: `server/sessions/remote.py` (`RemoteDriver.seed_idle_suggestions`)
- Modify: `server/sessions/orchestrator.py` (add `seed_idle_suggestions(session_id, user_text, assistant_text)` delegating to the live driver)
- Test: `server/tests/test_remote_driver.py`, `server/tests/test_orchestrator_rpc_breadth.py`

**Interfaces:**
- Produces: `RemoteOrchestrator`/`SessionOrchestrator.seed_idle_suggestions(session_id, user_text, assistant_text) -> None`; daemon RPC `seed_idle_suggestions`; `RemoteDriver.seed_idle_suggestions(user_text, assistant_text)` (sync signature matching the in-process driver — fire-and-forget via the client).

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_remote_driver.py  (append)
import pytest

@pytest.mark.asyncio
async def test_remote_driver_seed_idle_suggestions_calls_rpc(fake_daemon_client):
    from server.sessions.remote import RemoteDriver
    d = RemoteDriver(fake_daemon_client, "sess", status="idle")
    d.seed_idle_suggestions("hi", "there")
    assert fake_daemon_client.calls[-1] == (
        "seed_idle_suggestions",
        {"session_id": "sess", "user_text": "hi", "assistant_text": "there"})
```

> `fake_daemon_client` records `.call(method, **params)` into `.calls`; reuse the
> fake used by this file's other RemoteDriver/RemoteOrchestrator tests. If
> `seed_idle_suggestions` must be sync (it is called from `kick_idle_suggestions`,
> a sync function), schedule the RPC with `asyncio.create_task` inside RemoteDriver.

- [ ] **Step 2: Run test to verify it fails**

Run: `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_remote_driver.py -k seed_idle -v`
Expected: FAIL (`AttributeError: 'RemoteDriver' object has no attribute 'seed_idle_suggestions'`).

- [ ] **Step 3: Write minimal implementation**

```python
# server/sessions/remote.py  — in RemoteDriver:
    def seed_idle_suggestions(self, user_text: str, assistant_text: str) -> None:
        # Fire-and-forget: the daemon's real driver generates suggestions and the
        # resulting Suggestions event flows back over this session's event stream.
        import asyncio
        asyncio.create_task(self._client.call(
            "seed_idle_suggestions", session_id=self._session_id,
            user_text=user_text, assistant_text=assistant_text))

# server/sessions/orchestrator.py  — add:
    def seed_idle_suggestions(self, session_id, user_text, assistant_text) -> None:
        d = self._drivers.get(session_id)
        if d is not None:
            d.seed_idle_suggestions(user_text, assistant_text)

# server/ipc/orchestrator_rpc.py  — add a handler (returns {"ok": True}):
    async def seed_idle_suggestions(params: dict) -> dict:
        orch.seed_idle_suggestions(params["session_id"], params["user_text"], params["assistant_text"])
        return {"ok": True}
    register_rpc(app, "seed_idle_suggestions", seed_idle_suggestions)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_remote_driver.py -k seed_idle -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/sessions/remote.py server/sessions/orchestrator.py server/ipc/orchestrator_rpc.py server/tests/test_remote_driver.py
git commit -m "feat(remote): RemoteDriver.seed_idle_suggestions via daemon RPC (remote-mode parity)"
```

### Task 2.2: Guard against future RemoteDriver surface drift

**Files:**
- Test: `server/tests/test_remote_driver.py`

**Interfaces:**
- Consumes: `RemoteDriver`, the set of `driver.*` attributes accessed in `server/bridge/ws.py` and `server/sessions/suggestions.py`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_remote_driver.py  (append)
def test_remote_driver_covers_every_driver_method_ws_calls():
    import re, pathlib
    from server.sessions.remote import RemoteDriver
    text = ""
    for p in ("server/bridge/ws.py", "server/sessions/suggestions.py"):
        text += pathlib.Path(p).read_text()
    called = set(re.findall(r"\bdriver\.(\w+)", text))
    # exclude private/dunder accessed only inside comments/docstrings false-positives
    called = {c for c in called if not c.startswith("_")}
    missing = {c for c in called if not hasattr(RemoteDriver("c", "s"), c)}
    assert not missing, f"RemoteDriver missing methods ws.py calls: {sorted(missing)}"
```

> `RemoteDriver("c", "s")` — client, session_id positional. If construction needs
> more, build it the way other tests in this file do.

- [ ] **Step 2: Run test to verify it fails or passes**

Run: `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_remote_driver.py -k covers_every -v`
Expected: PASS after Task 2.1 (this is a regression guard). If it FAILS, it has found another missing method — add it to `RemoteDriver` (RPC-forward or documented no-op) before proceeding.

- [ ] **Step 3: (impl only if the guard found gaps — forward each via RPC like 2.1)**

- [ ] **Step 4: Re-run**

Run: `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_remote_driver.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/sessions/remote.py server/tests/test_remote_driver.py
git commit -m "test(remote): assert RemoteDriver covers every driver method ws.py calls"
```

---

## Gap 3 — Graceful command / DaemonError handling

### Task 3.1: `run_command` returns a clean notice instead of raising

**Files:**
- Modify: `server/sessions/orchestrator.py` (`run_command`, ~line 1006)
- Test: `server/tests/test_command_orchestrator.py`

**Interfaces:**
- Produces: `run_command` returns `{"name": name, "render": "notice", "data": {"text": ...}}` for a not-live session or unknown command instead of raising `KeyError`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_command_orchestrator.py  (append)
import pytest

@pytest.mark.asyncio
async def test_run_command_unknown_session_returns_notice(make_orchestrator):
    orch = make_orchestrator()  # no drivers registered
    res = await orch.run_command("nope", "account", "primary")
    assert res["render"] == "notice"
    assert "not" in res["data"]["text"].lower()  # "session not live" / similar
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_command_orchestrator.py -k unknown_session -v`
Expected: FAIL (raises `KeyError`).

- [ ] **Step 3: Write minimal implementation**

```python
# server/sessions/orchestrator.py  — run_command, replace the leading guard:
        d = self._drivers.get(session_id)
        if d is None:
            return {"name": str(name).lstrip("/"), "render": "notice",
                    "data": {"text": "This session isn't live — open it and try again."}}
# ...and the unknown-command tail `if cmd is None: raise KeyError(name)` becomes:
        if cmd is None:
            return {"name": str(name).lstrip("/"), "render": "notice",
                    "data": {"text": f"Unknown command: /{str(name).lstrip('/')}"}}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_command_orchestrator.py server/tests/test_command_driver.py server/tests/test_account_command.py -v`
Expected: PASS (no regressions in command tests).

- [ ] **Step 5: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_command_orchestrator.py
git commit -m "fix(sessions): run_command returns a notice instead of raising KeyError"
```

### Task 3.2: Global `DaemonError` → clean HTTP response

**Files:**
- Modify: `server/app.py` (register an exception handler)
- Test: `server/tests/test_remote_mode_edge.py`

**Interfaces:**
- Consumes: `server.ipc.client.DaemonError`, `DaemonUnavailable`.
- Produces: a FastAPI exception handler mapping `DaemonError` → HTTP 409 `{"error": "...", "type": "..."}` and `DaemonUnavailable` → HTTP 503, so no daemon RPC failure surfaces as an uncaught 500.

- [ ] **Step 1: Investigate (no code) — identify the 500ing REST path**

Run: `grep -rn "run_command\|ensure_driver\|\.call(" server/api server/app.py | grep -v test`
Note which REST handler reached the daemon in the incident trace (passed through `server/auth/middleware.py`). The handler below is path-agnostic, so this only confirms coverage.

- [ ] **Step 2: Write the failing test**

```python
# server/tests/test_remote_mode_edge.py  (append; reuse this file's app-with-fake-daemon harness)
def test_daemonerror_maps_to_409_not_500(client_with_failing_daemon):
    # the fake daemon raises DaemonError on the RPC this endpoint calls
    resp = client_with_failing_daemon.get("/api/sessions/does-not-exist")  # or the real 500ing route
    assert resp.status_code in (409, 503)
    assert resp.status_code != 500
```

> Use whichever REST route Step 1 identifies as reaching the daemon. Reuse the
> existing remote-mode app harness in this file (it already builds an app wired to a
> fake `DaemonClient`); make that fake raise `DaemonError("KeyError", "account")`.

- [ ] **Step 3: Run test to verify it fails**

Run: `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_remote_mode_edge.py -k daemonerror -v`
Expected: FAIL (500).

- [ ] **Step 4: Write minimal implementation**

```python
# server/app.py  — inside create_app(), after app is constructed:
    from fastapi import Request
    from fastapi.responses import JSONResponse
    from server.ipc.client import DaemonError, DaemonUnavailable

    @app.exception_handler(DaemonUnavailable)
    async def _on_daemon_unavailable(request: "Request", exc: DaemonUnavailable):
        return JSONResponse({"error": "session daemon unavailable", "type": "DaemonUnavailable"},
                            status_code=503)

    @app.exception_handler(DaemonError)
    async def _on_daemon_error(request: "Request", exc: DaemonError):
        return JSONResponse({"error": exc.message, "type": exc.err_type}, status_code=409)
```

- [ ] **Step 5: Run test to verify it passes & commit**

Run: `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_remote_mode_edge.py -v`
Expected: PASS.

```bash
git add server/app.py server/tests/test_remote_mode_edge.py
git commit -m "fix(edge): map DaemonError/DaemonUnavailable to 409/503 instead of uncaught 500"
```

---

## Final verification (before opening PR(s))

- [ ] Run the full server suite: `env -u CCSWITCH_ENABLE_XVFB /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/ -q`
- [ ] Confirm no `RemoteDriver` AttributeErrors and no `DaemonError`→500 paths remain.
- [ ] Open PR(s) per CLAUDE.md completion workflow (non-draft, `--squash --auto`). Gap 1+4 may be one PR (shared recovery infra); Gaps 2 and 3 each standalone, or all four together if review prefers.

## Self-review notes (author)

- Spec coverage: Gap 1 → Tasks 1.1–1.4; Gap 4 → 4.1–4.2; Gap 2 → 2.1–2.2; Gap 3 → 3.1–3.2. All four covered.
- Fixtures named (`make_orchestrator_with_live_session`, `make_driver`, `fake_daemon_client`, `make_orchestrator`, harnesses) are assumed to mirror existing ones in the named test files; each task notes "reuse the existing fake — don't invent one." First task touching a file factors the shared fixture if absent.
- Type consistency: `process_crash` kind, `record_crash`/`crash_cap_reached`, `last_progress_at`/`_note_progress`, `seed_idle_suggestions(session_id, user_text, assistant_text)` used identically across tasks.
