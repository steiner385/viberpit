# Auto-resend a dropped turn — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a live turn dies with `[ede_diagnostic] … stop_reason=null` (the model dropped the turn before committing any continuation), automatically resend the user's last message on the same account — capped and idempotent — instead of dead-ending on a red error.

**Architecture:** Reuse the existing transient-failure recovery pipeline (`failure.classify` → `driver._maybe_signal_recovery` → `orchestrator._recover_session` → `_replay_same_account`). Add a new recoverable kind `turn_dropped`, a per-session resend cap in `RecoveryState`, and route the raw (pre-humanize) error text to the classifier so softening the banner does not blind the classifier.

**Tech Stack:** Python 3, asyncio, pytest. Backend only — no frontend or wire changes.

## Global Constraints

- Single source of truth / no parallel implementations (constitution VI): route recovery through the existing pipeline; do **not** add a second retry path.
- New model columns must be nullable — N/A here (no schema changes).
- Backend lint gates run first in CI: `ruff` + `mypy` must pass before tests.
- The classifier MUST gate on the exact token `stop_reason=null` (never `last_content_type=n/a`, which both ede variants share, and never merely "not `tool_use`").
- Resend cap: **3 within 120s** per session.
- Run backend tests with the prebuilt env + worktree src:
  `PYTHONPATH=<worktree>/src <main-checkout>/.venv/bin/python -m pytest server/tests/…`

---

### Task 1: Classify the dropped turn as `turn_dropped`

**Files:**
- Modify: `server/sessions/failure.py`
- Test: `server/tests/test_failure_classify.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `classify(code, message)` returns `TurnFailure(kind="turn_dropped")` for the dropped-turn signature; `"turn_dropped"` is in `_RECOVERABLE` so `.is_recoverable` is `True`.

- [ ] **Step 1: Write the failing tests**

Add to `server/tests/test_failure_classify.py`:

```python
def test_dropped_turn_ede_is_recoverable_turn_dropped():
    f = classify("execution_error",
                 "[ede_diagnostic] result_type=user last_content_type=n/a stop_reason=null")
    assert f.kind == "turn_dropped"
    assert f.is_recoverable


def test_orphaned_tool_use_ede_is_not_turn_dropped():
    # the tool_use variant is healed by transcript_repair on resume, NOT auto-resent
    f = classify("execution_error",
                 "[ede_diagnostic] result_type=user last_content_type=n/a stop_reason=tool_use")
    assert f.kind != "turn_dropped"
    assert not f.is_recoverable


def test_plain_execution_error_is_not_turn_dropped():
    f = classify("execution_error", "The agent stopped on an execution error.")
    assert f.kind == "other"
    assert not f.is_recoverable
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH="$PWD/src" python -m pytest server/tests/test_failure_classify.py -q`
Expected: the three new tests FAIL (`turn_dropped` not produced; first asserts `kind == "turn_dropped"`).

- [ ] **Step 3: Implement the classifier branch**

In `server/sessions/failure.py`, extend `_RECOVERABLE`:

```python
_RECOVERABLE = frozenset({"rate_limit", "auth_401", "process_crash", "turn_dropped"})
```

In `classify()`, add immediately after the `_OVERLOAD_CODES` branch (after `return TurnFailure(kind="overloaded")`) and before the process-crash check:

```python
    # A turn that dropped before the model committed any continuation — the CLI tags it
    # "[ede_diagnostic] ... stop_reason=null". Distinct from the orphaned-tool_use variant
    # (stop_reason=tool_use), which transcript_repair heals on resume. Nothing was
    # committed, so re-sending the same prompt is idempotent and safe.
    if code == "execution_error" and "[ede_diagnostic]" in text and "stop_reason=null" in text:
        return TurnFailure(kind="turn_dropped")
```

(Inlining the `[ede_diagnostic]` / `stop_reason=null` signature strings matches this file's existing idiom — it already inlines `"429"`, `"401"`, `"overloaded"` as recognized signatures rather than shared config.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH="$PWD/src" python -m pytest server/tests/test_failure_classify.py -q`
Expected: PASS (all, including the pre-existing `test_unrelated_error_is_other_and_not_recoverable`, which uses a plain execution_error with no ede marker → still `other`).

- [ ] **Step 5: Commit**

```bash
git add server/sessions/failure.py server/tests/test_failure_classify.py
git commit -m "feat(failure): classify [ede_diagnostic] stop_reason=null as recoverable turn_dropped

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Per-session resend cap in `RecoveryState`

**Files:**
- Modify: `server/sessions/recovery.py`
- Test: `server/tests/test_recovery_state.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `RESEND_CAP: int = 3`, `RESEND_WINDOW: float = 120.0`, and on `RecoveryState`:
  `record_resend(session_id, *, now)` / `resend_cap_reached(session_id, *, now) -> bool`
  (windowed, prune-on-read — same shape as `record_crash` / `crash_cap_reached`).

- [ ] **Step 1: Write the failing tests**

Add to `server/tests/test_recovery_state.py`:

```python
def test_resend_cap_reached_after_max_within_window():
    s = RecoveryState()
    for t in (0.0, 1.0, 2.0):
        assert not s.resend_cap_reached("sess", now=t)
        s.record_resend("sess", now=t)
    assert s.resend_cap_reached("sess", now=3.0)


def test_resends_outside_window_do_not_count():
    s = RecoveryState()
    s.record_resend("sess", now=0.0)
    s.record_resend("sess", now=10.0)
    s.record_resend("sess", now=20.0)
    assert s.resend_cap_reached("sess", now=30.0)
    assert not s.resend_cap_reached("sess", now=200.0)


def test_resend_does_not_consume_crash_budget():
    s = RecoveryState()
    for _ in range(3):
        s.record_resend("sess", now=1000.0)
    assert not s.crash_cap_reached("sess", now=1000.0)  # separate budgets
```

(If `RecoveryState` is not already imported at the top of the test file, add `from server.sessions.recovery import RecoveryState` — check the existing imports first and reuse them.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH="$PWD/src" python -m pytest server/tests/test_recovery_state.py -q`
Expected: FAIL with `AttributeError: 'RecoveryState' object has no attribute 'record_resend'`.

- [ ] **Step 3: Implement the cap**

In `server/sessions/recovery.py`, add the tunables next to `CRASH_CAP` / `CRASH_WINDOW`:

```python
RESEND_CAP: int = 3              # auto-resends of a dropped turn per session within RESEND_WINDOW
RESEND_WINDOW: float = 120.0     # window (seconds) for tracking dropped-turn resends
```

In `RecoveryState.__init__`, after the stall fields:

```python
        self._resends: dict[str, list[float]] = defaultdict(list)  # session_id -> resend timestamps
        self._resend_cap = RESEND_CAP
        self._resend_window = RESEND_WINDOW
```

Add the methods after `stall_cap_reached` (mirror the crash methods exactly):

```python
    # --- per-session dropped-turn resend cap ----------------------------------
    def record_resend(self, session_id: str, *, now: float) -> None:
        self._resends[session_id].append(now)

    def resend_cap_reached(self, session_id: str, *, now: float) -> bool:
        """True if *session_id* has already auto-resent ``resend_cap`` dropped turns
        within the window."""
        cutoff = now - self._resend_window
        recent = [t for t in self._resends.get(session_id, ()) if t > cutoff]
        self._resends[session_id] = recent
        return len(recent) >= self._resend_cap
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH="$PWD/src" python -m pytest server/tests/test_recovery_state.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/sessions/recovery.py server/tests/test_recovery_state.py
git commit -m "feat(recovery): per-session resend cap (3/120s) for dropped turns

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Carry raw error text + soften the dropped-turn banner

**Files:**
- Modify: `server/sessions/events.py` (`ErrorEvent`)
- Modify: `server/sessions/translator.py` (`_error_from_result`, `_humanize`)
- Test: `server/tests/test_translator.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `ev.ErrorEvent` gains an internal field `raw_message: str | None = None` (the pre-humanize text; **not** included in `to_wire`). For the `stop_reason=null` variant, `ErrorEvent.message` is humanized to an auto-resend notice while `raw_message` still contains the `[ede_diagnostic] … stop_reason=null` signal.

- [ ] **Step 1: Write the failing tests**

Add to `server/tests/test_translator.py`:

```python
def test_ede_dropped_turn_gets_auto_resend_message_and_keeps_raw():
    msgs = [f.stream_start(uuid="t1"),
            f.result(uuid="t1", subtype="error_during_execution", is_error=True,
                     errors=["[ede_diagnostic] result_type=user last_content_type=n/a "
                             "stop_reason=null"])]
    _, out = _run(msgs)
    err = next(e for e in out if isinstance(e, ErrorEvent))
    assert err.code == "execution_error"
    # display text is softened
    assert not err.message.startswith("[ede_diagnostic]")
    assert "resend" in err.message.lower()
    # raw diagnostic text is preserved for classification
    assert err.raw_message is not None
    assert "[ede_diagnostic]" in err.raw_message and "stop_reason=null" in err.raw_message
    # raw text is internal — never serialized to the wire
    assert "raw" not in err.to_wire()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH="$PWD/src" python -m pytest server/tests/test_translator.py -q`
Expected: FAIL — `AttributeError: 'ErrorEvent' object has no attribute 'raw_message'`.

- [ ] **Step 3a: Add the `raw_message` field to `ErrorEvent`**

In `server/sessions/events.py`, update the `ErrorEvent` dataclass — add `raw_message` **after** `ts` (so positional construction sites are unaffected) and leave `to_wire` unchanged:

```python
@dataclass
class ErrorEvent:
    code: str; message: str; resets_at: str | None = None; turn_id: str | None = None
    ts: float | None = field(default_factory=_now_ms)
    raw_message: str | None = None  # pre-humanize diagnostic text; internal, not wired
    def to_wire(self): return {"type": "error", "code": self.code, "message": self.message,
                               "resetsAt": self.resets_at, "turnId": self.turn_id, "ts": self.ts}
```

- [ ] **Step 3b: Populate `raw_message` and soften the dropped banner**

In `server/sessions/translator.py`, change the return of `_error_from_result` to pass the pre-humanize `message` as `raw_message`:

```python
    return ev.ErrorEvent(code=code, message=_humanize(code, message),
                         turn_id=turn_id, raw_message=message)
```

Add the dropped-turn human message next to `_EDE_HUMAN`:

```python
_EDE_HUMAN_DROPPED = ("The turn ended before the model responded — resending your last "
                      "message automatically to continue.")
```

Replace `_humanize` with a version that handles both ede variants:

```python
def _humanize(code: str, message: str) -> str:
    if code == "execution_error" and _EDE_MARKER in message:
        if "stop_reason=tool_use" in message:
            return _EDE_HUMAN
        if "stop_reason=null" in message:
            return _EDE_HUMAN_DROPPED
    return message
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH="$PWD/src" python -m pytest server/tests/test_translator.py -q`
Expected: PASS — including the pre-existing `test_ede_orphaned_tool_use_gets_a_legible_message` (tool_use → `_EDE_HUMAN`, contains "interrupted"/"tool") and `test_unrelated_execution_error_keeps_its_raw_text` (no ede marker → unchanged).

- [ ] **Step 5: Commit**

```bash
git add server/sessions/events.py server/sessions/translator.py server/tests/test_translator.py
git commit -m "feat(translator): carry raw error text; soften dropped-turn banner

ErrorEvent.raw_message preserves the pre-humanize [ede_diagnostic] signal (internal,
not wired) so the classifier still sees stop_reason=null after the banner is softened.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Driver classifies on raw error text

**Files:**
- Modify: `server/sessions/driver.py` (`_drain` call to `_maybe_signal_recovery`)
- Test: `server/tests/test_driver_recovery.py`

**Interfaces:**
- Consumes: `ErrorEvent.raw_message` (Task 3), `classify` → `turn_dropped` (Task 1).
- Produces: a dropped-turn `ResultMessage` drives `on_recoverable_failure(session_id, "turn_dropped", None)` and emits `TurnFailure(kind="turn_dropped")` — **even though the banner is humanized** — because the driver classifies on `raw_message`.

- [ ] **Step 1: Write the failing test**

Add to `server/tests/test_driver_recovery.py`:

```python
def test_dropped_turn_result_invokes_recovery_as_turn_dropped():
    msgs = [f.sys_init("sess-1"), f.stream_start(uuid="t1"),
            f.result(session_id="sess-1", uuid="t1", subtype="error_during_execution",
                     is_error=True,
                     errors=["[ede_diagnostic] result_type=user last_content_type=n/a "
                             "stop_reason=null"])]
    seen, calls = _run(msgs)
    assert calls and calls[0][0] == "s1" and calls[0][1] == "turn_dropped"
    tf = [e for e in seen if isinstance(e, ev.TurnFailure)]
    assert tf and tf[0].kind == "turn_dropped" and tf[0].account == "kindash"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH="$PWD/src" python -m pytest server/tests/test_driver_recovery.py::test_dropped_turn_result_invokes_recovery_as_turn_dropped -q`
Expected: FAIL — the driver currently classifies the **humanized** message (no `[ede_diagnostic]`/`stop_reason=null` token), so `classify` returns `other` and no callback fires (`calls == []`). This proves the ordering bug Task 4 fixes.

- [ ] **Step 3: Route raw text to the classifier**

In `server/sessions/driver.py`, in `_drain`, change the `ErrorEvent` branch:

```python
                    elif isinstance(e, ev.ErrorEvent):
                        await self._maybe_signal_recovery(e.code, e.raw_message or e.message)
```

(`raw_message or e.message` keeps the drain-fallback `driver_error` path — which constructs `ErrorEvent` without `raw_message` — working unchanged.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH="$PWD/src" python -m pytest server/tests/test_driver_recovery.py -q`
Expected: PASS (new test + all existing 429/401/crash tests, which classify by `code` and are unaffected).

- [ ] **Step 5: Commit**

```bash
git add server/sessions/driver.py server/tests/test_driver_recovery.py
git commit -m "fix(driver): classify recovery on raw error text, not the humanized banner

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: Orchestrator recovers a `turn_dropped` failure

**Files:**
- Modify: `server/sessions/orchestrator.py` (`_recover_session`)
- Test: `server/tests/test_orchestrator_recovery.py`

**Interfaces:**
- Consumes: `turn_dropped` kind (Task 1); `record_resend` / `resend_cap_reached` (Task 2); existing `_replay_same_account(record, retry_after)`.
- Produces: a `turn_dropped` failure triggers exactly one `_replay_same_account(record, 0.0)` per event until the resend cap is hit; on cap it stops with no rotation/quarantine/pause.

- [ ] **Step 1: Write the failing tests**

Add to `server/tests/test_orchestrator_recovery.py`:

```python
def test_turn_dropped_replays_same_account_immediately():
    o = _make_orch(); rec = _rec(o)
    asyncio.run(o._recover_session(rec, "turn_dropped", None))
    assert o.calls == [("replay", "s1", 0.0)]
    # not an account fault → nothing quarantined
    assert o._recovery.quarantined(now=1000.0) == frozenset()


def test_turn_dropped_stops_after_resend_cap():
    o = _make_orch(); rec = _rec(o)
    for _ in range(R.RESEND_CAP):
        o._recovery.record_resend("s1", now=1000.0)
    asyncio.run(o._recover_session(rec, "turn_dropped", None))
    assert o.calls == []  # cap reached → no resend, no pause/rotate
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH="$PWD/src" python -m pytest server/tests/test_orchestrator_recovery.py -q`
Expected: FAIL — `_recover_session` has no `turn_dropped` branch, so it falls through and `o.calls` stays `[]` for the first test.

- [ ] **Step 3: Implement the recovery branch**

In `server/sessions/orchestrator.py`, in `_recover_session`, insert the branch immediately after `now = self._clock()` and **before** the `rotation_cap_reached` check (a dropped turn is independent of rotation budget):

```python
        if kind == "turn_dropped":
            # The turn dropped before the model committed any continuation. Resend the last
            # prompt on the same account (idempotent — nothing was committed), bounded so a
            # persistent drop can't loop. Not an account fault → no rotation/quarantine/pause.
            if self._recovery.resend_cap_reached(record.id, now=now):
                return
            self._recovery.record_resend(record.id, now=now)
            await self._replay_same_account(record, 0.0)
            return
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH="$PWD/src" python -m pytest server/tests/test_orchestrator_recovery.py -q`
Expected: PASS (new tests + all existing recovery dispatch tests).

- [ ] **Step 5: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_orchestrator_recovery.py
git commit -m "feat(orchestrator): auto-resend the last prompt on a dropped turn (capped)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: Full backend gate (lint + suite)

**Files:** none (verification only).

- [ ] **Step 1: Lint**

Run: `ruff check server/sessions/failure.py server/sessions/recovery.py server/sessions/translator.py server/sessions/events.py server/sessions/driver.py server/sessions/orchestrator.py`
Then: `mypy server/sessions/failure.py server/sessions/recovery.py server/sessions/translator.py server/sessions/events.py server/sessions/driver.py server/sessions/orchestrator.py`
Expected: clean (mypy skips untyped function bodies).

- [ ] **Step 2: Run the affected suites green**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_ENABLE_XVFB= CCSWITCH_DEV=1 PYTHONPATH="$PWD/src" python -m pytest server/tests/test_failure_classify.py server/tests/test_recovery_state.py server/tests/test_translator.py server/tests/test_driver_recovery.py server/tests/test_orchestrator_recovery.py -q`
Expected: all PASS. (Unset `CCSWITCH_ENABLE_XVFB` to match CI per project norms; the two known env-only failures — `remote_mode_wiring`, `x11` — are out of scope and not in this set.)

- [ ] **Step 3: No commit** — verification task only. Proceed to PR per the project completion workflow (branch already `feat/auto-resend-dropped-turn`; open non-draft, `--squash --auto`).

---

## Self-Review

**Spec coverage:**
- Classify `turn_dropped` (spec §1) → Task 1. ✓
- Resend cap 3/120s (spec §2) → Task 2. ✓
- Orchestrator immediate same-account resend, no rotation on cap (spec §3) → Task 5. ✓
- Soften banner for `stop_reason=null` (spec §4) → Task 3. ✓
- Recovery indicator (spec §5): spec states the softened banner is the minimum and sufficient on its own → delivered by Task 3; no separate indicator task (honest scope: no extra in-turn flag is added, matching the spec's deferral). ✓
- Ordering bug (humanize strips the classify signal) — not in the original spec prose; covered by Task 3 (`raw_message`) + Task 4 (driver classifies on raw). The end-to-end driver test in Task 4 is the regression guard. ✓

**Placeholder scan:** none — every step shows concrete code/commands.

**Type consistency:** `record_resend` / `resend_cap_reached` (Task 2) used verbatim in Task 5; `raw_message` field (Task 3) consumed verbatim in Task 4; `turn_dropped` kind string identical across Tasks 1/4/5; `_replay_same_account(record, retry_after)` signature matches the existing method and the test spy.
