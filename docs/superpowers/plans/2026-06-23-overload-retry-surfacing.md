# Overload-retry surfacing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a transient upstream `529 Overloaded` (and 5xx) self-heal instead of becoming a dead session — the stall watchdog must not abort an actively-retrying turn, and the UI must say "Overloaded — retrying" / give an honest terminal hint instead of generic "Turn failed".

**Architecture:** The claude-agent-sdk yields each mid-turn retry as `SystemMessage(subtype="api_error", data={...})`. The translator (which today drops all `SystemMessage`s) gains a handler that emits a new **internal-only** `ev.ApiRetry` event (modeled on `ev.RateLimit`: no `to_wire`, never forwarded to the client). The driver counts `ApiRetry` as watchdog progress and flips a transient `_retrying` flag that `derive_status` turns into a new `"retrying"` status, which rides the existing `status` wire event to the frontend's single `statusTier` map. At turn end, an overload result is classified `kind="overloaded"` (not recoverable → no rotation) and surfaced with overload-specific copy in the existing transcript error card.

**Tech Stack:** Python (FastAPI/asyncio, dataclasses) backend in `server/sessions/`; React/TS + Vitest frontend in `web/src/session/`. TDD with pytest + vitest.

## Global Constraints

- **No parallel implementations.** Extend the existing source-of-truth in place: `failure.classify` (failure kinds), `events.py` (event dataclasses), `derive_status`/`StatusSignals` (status derivation), `statusTier.ts` `PRESENTATION` (status→label map). Do NOT add a second status map or a parallel retry tracker.
- **Overload is NOT recoverable-by-rotation.** `kind="overloaded"` must NOT be added to `failure._RECOVERABLE`. Rotating to another equally-overloaded account is pointless and burns the rotation cap.
- **`ApiRetry` is internal-only.** Like `ev.RateLimit`, it has no `to_wire()` and is never put on `_out` / forwarded to the client. The live "retrying" state reaches the UI only via the existing `status` event.
- **Overload status set:** HTTP `{500, 502, 503, 529}`. (429 stays `rate_limit`, 401 stays `auth_401` — unchanged.)
- **Backend test invocation (worktree):** the venv's editable `ccswitch` resolves to the stale deploy checkout, so set PYTHONPATH to this worktree's `src`. Run from the worktree root:
  ```bash
  env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_ENABLE_XVFB CCSWITCH_DEV=1 \
    PYTHONPATH=$PWD/src \
    /mnt/ssk-ssd/tony/GitHub/cc-account-switcher-deploy/.venv/bin/python -m pytest server/tests/<file> -v
  ```
- **Web test invocation:** run inside `web/` with the symlinked `node_modules` (symlink from the deploy worktree if absent) using the local bin, not npx:
  ```bash
  cd web && ./node_modules/.bin/vitest run src/session/<file>
  ```
  Real typecheck is `./node_modules/.bin/tsc -b --force` (NOT `tsc --noEmit`, which checks nothing here).
- **Commit cadence:** one commit per task (after its tests pass). End every commit message with the `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>` trailer.

---

## File Structure

- `server/sessions/failure.py` — add `overloaded` kind to `classify` (NOT recoverable). [Task 1]
- `server/sessions/events.py` — add internal `ApiRetry` dataclass. [Task 2]
- `server/sessions/translator.py` — `SystemMessage(api_error)` → `ApiRetry`; overload-specific terminal message. [Task 2]
- `server/sessions/driver.py` — `_note_progress` counts `ApiRetry`; `_drain` flips `_retrying` + emits status; `_dash_signals` exposes it. [Task 3]
- `server/sessions/status.py` — `StatusSignals.retrying` + `derive_status` `"retrying"`. [Task 4]
- `web/src/session/statusTier.ts` — `retrying` entry in `PRESENTATION`. [Task 5]
- Tests: `server/tests/test_failure_classify.py`, `test_translator.py`, `test_driver_stall.py`, `test_status.py`; `web/src/session/statusTier.test.ts` (create if absent).

---

### Task 1: Classify overload as a non-recoverable `overloaded` kind

**Files:**
- Modify: `server/sessions/failure.py`
- Test: `server/tests/test_failure_classify.py` (add cases; file already exists)

**Interfaces:**
- Consumes: `classify(code: str | None, message: str = "", *, retry_after=None) -> TurnFailure`
- Produces: `TurnFailure(kind="overloaded")` with `is_recoverable is False` for codes `api_500/502/503/529` and 5xx/"overloaded" messages.

- [ ] **Step 1: Write the failing test**

Add to `server/tests/test_failure_classify.py`:

```python
import pytest
from server.sessions.failure import classify

@pytest.mark.parametrize("code", ["api_500", "api_502", "api_503", "api_529"])
def test_overload_codes_classify_overloaded_not_recoverable(code):
    f = classify(code, "529 Overloaded")
    assert f.kind == "overloaded"
    assert f.is_recoverable is False

def test_overload_message_fallback_without_code():
    f = classify("driver_error", '529 {"type":"error","error":{"type":"overloaded_error"}}')
    assert f.kind == "overloaded"

def test_429_still_rate_limit_not_overloaded():
    assert classify("api_429", "429 rate limit").kind == "rate_limit"

def test_401_still_auth_not_overloaded():
    assert classify("api_401", "401 Invalid authentication credentials").kind == "auth_401"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_ENABLE_XVFB CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher-deploy/.venv/bin/python -m pytest server/tests/test_failure_classify.py -v`
Expected: FAIL — overload cases return `kind="other"`.

- [ ] **Step 3: Write minimal implementation**

In `server/sessions/failure.py`, add the overload status set near the top constants (after `_RECOVERABLE`):

```python
_OVERLOAD_STATUSES = frozenset({500, 502, 503, 529})
_OVERLOAD_CODES = frozenset(f"api_{s}" for s in _OVERLOAD_STATUSES)
```

In `classify()`, add the overload branch AFTER the `api_429` / `api_401` code checks and BEFORE the process-crash check:

```python
    if code == "api_429":
        return rate_limit()
    if code == "api_401":
        return TurnFailure(kind="auth_401")
    if code in _OVERLOAD_CODES:
        return TurnFailure(kind="overloaded")
```

Then extend the content-inference fallback (after the `is_401` / `is_429` block, before `return TurnFailure(kind="other")`):

```python
    is_overload = "overloaded" in low or any(str(s) in text for s in _OVERLOAD_STATUSES)
    if is_overload:
        return TurnFailure(kind="overloaded")
    return TurnFailure(kind="other")
```

Leave `_RECOVERABLE` unchanged — `overloaded` is intentionally absent (no rotation).

- [ ] **Step 4: Run tests to verify they pass**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_ENABLE_XVFB CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher-deploy/.venv/bin/python -m pytest server/tests/test_failure_classify.py -v`
Expected: PASS (all, including the pre-existing 429/401 cases).

- [ ] **Step 5: Commit**

```bash
git add server/sessions/failure.py server/tests/test_failure_classify.py
git commit -m "feat(failure): classify 5xx/529 as non-recoverable 'overloaded' kind

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: `ApiRetry` event + translator handler + honest terminal copy

**Files:**
- Modify: `server/sessions/events.py` (add `ApiRetry`)
- Modify: `server/sessions/translator.py` (`SystemMessage` handler; overload terminal message)
- Test: `server/tests/test_translator.py` (add cases; file already exists)

**Interfaces:**
- Produces: `ev.ApiRetry(turn_id: str, status: int | None, attempt: int | None, max_retries: int | None)` — internal, no `to_wire`.
- Translator emits `[ev.ApiRetry(...)]` for `SystemMessage(subtype="api_error")`; emits `ev.ErrorEvent(code="api_<status>", message=<overload copy>)` for an overload `ResultMessage`.

- [ ] **Step 1: Write the failing test**

Add to `server/tests/test_translator.py` (match the existing import/fixture style in that file):

```python
from claude_agent_sdk.types import SystemMessage
from server.sessions import events as ev
from server.sessions.translator import Translator

def _api_error_msg(status=529, attempt=3, max_retries=20):
    return SystemMessage(subtype="api_error", data={
        "type": "system", "subtype": "api_error",
        "error": {"status": status, "formatted": f"{status} Overloaded"},
        "retryAttempt": attempt, "maxRetries": max_retries,
    })

def test_translator_emits_apiretry_for_midturn_api_error():
    tr = Translator()
    out = tr.feed(_api_error_msg())
    assert len(out) == 1
    e = out[0]
    assert isinstance(e, ev.ApiRetry)
    assert e.status == 529 and e.attempt == 3 and e.max_retries == 20

def test_apiretry_has_no_wire_form():
    assert not hasattr(ev.ApiRetry(turn_id="t", status=529, attempt=1, max_retries=20), "to_wire")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_ENABLE_XVFB CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher-deploy/.venv/bin/python -m pytest server/tests/test_translator.py -k apiretry -v`
Expected: FAIL — `ev.ApiRetry` does not exist; translator returns `[]` for `SystemMessage`.

- [ ] **Step 3a: Add the `ApiRetry` dataclass**

In `server/sessions/events.py`, add next to `RateLimit` (mirror its "internal carrier, no wire form" note):

```python
@dataclass
class ApiRetry:
    """Decoded SDK SystemMessage(subtype='api_error'). Internal carrier only: the
    driver consumes it to keep the stall watchdog alive during backoff and to flag
    the transient 'retrying' status. Never forwarded to the client (no wire form)."""
    turn_id: str; status: int | None = None; attempt: int | None = None; max_retries: int | None = None
```

- [ ] **Step 3b: Handle `SystemMessage(api_error)` in the translator**

In `server/sessions/translator.py`, ensure `SystemMessage` is imported from `claude_agent_sdk.types` alongside the other message types, then add this handler in `Translator.feed()` BEFORE the final `return []` (e.g. just after the `RateLimitEvent` branch):

```python
        if isinstance(msg, SystemMessage) and getattr(msg, "subtype", None) == "api_error":
            data = getattr(msg, "data", None) or {}
            err = data.get("error") or {}
            tid = self._turn_ids.get(None) or "turn"
            return [ev.ApiRetry(
                turn_id=tid,
                status=err.get("status"),
                attempt=data.get("retryAttempt"),
                max_retries=data.get("maxRetries"),
            )]
```

- [ ] **Step 3c: Overload-specific terminal message**

In `server/sessions/translator.py` `_error_from_result`, replace the `if api_status is not None:` default-message line so overload statuses get honest copy:

```python
    if api_status is not None:
        code = f"api_{api_status}"
        if api_status in (500, 502, 503, 529):
            default = (f"Anthropic is overloaded (HTTP {api_status}). The model API was "
                       f"retried and is still overloaded — re-send when capacity recovers.")
        else:
            default = f"The model API returned an error (HTTP {api_status})."
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_ENABLE_XVFB CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher-deploy/.venv/bin/python -m pytest server/tests/test_translator.py -v`
Expected: PASS (new cases + existing translator tests unaffected).

- [ ] **Step 5: Commit**

```bash
git add server/sessions/events.py server/sessions/translator.py server/tests/test_translator.py
git commit -m "feat(translator): emit internal ApiRetry for mid-turn api_error; overload terminal copy

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Watchdog counts retries as progress; driver flips `retrying`

**Files:**
- Modify: `server/sessions/driver.py` (`_note_progress`, `_drain`, `_dash_signals`, add `self._retrying`)
- Test: `server/tests/test_driver_stall.py` (create or extend the existing watchdog test file)

**Interfaces:**
- Consumes: `ev.ApiRetry` (Task 2), `StatusSignals(retrying=...)` (Task 4 adds the field — land Task 4 first OR add the field-default so this compiles; see Step 3).
- Produces: a driver whose `last_progress_at` advances on `ApiRetry`, and whose `_dash_signals().retrying` is True while the latest turn event is a retry.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_driver_stall.py` (or add to the existing stall test). Use the project's fake-driver/clock harness if present; otherwise test `_note_progress` directly:

```python
from server.sessions.driver import SessionDriver
from server.sessions import events as ev

def test_apiretry_counts_as_progress(make_driver):  # reuse existing driver fixture
    d = make_driver()
    t0 = d.last_progress_at
    d._clock = lambda: t0 + 100          # advance the injected clock
    d._note_progress(ev.ApiRetry(turn_id="t", status=529, attempt=2, max_retries=20))
    assert d.last_progress_at == t0 + 100

def test_empty_thinking_delta_still_not_progress(make_driver):
    d = make_driver()
    t0 = d.last_progress_at
    d._clock = lambda: t0 + 100
    d._note_progress(ev.ThinkingDelta(turn_id="t", text=""))
    assert d.last_progress_at == t0      # unchanged — wedged-turn guard intact
```

If there is no `make_driver` fixture, construct the driver the same way the existing `server/tests/` driver tests do (copy their setup) — do not invent a new harness.

- [ ] **Step 2: Run test to verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_ENABLE_XVFB CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher-deploy/.venv/bin/python -m pytest server/tests/test_driver_stall.py -v`
Expected: FAIL — `ApiRetry` not counted; `last_progress_at` stays `t0`.

- [ ] **Step 3: Implement**

In `server/sessions/driver.py` `__init__` (near `self._turn_active`), add:

```python
        self._retrying: bool = False
```

Extend `_note_progress` to count `ApiRetry` (add it to the meaningful-event branch):

```python
        elif isinstance(e, (ev.ToolCall, ev.ToolResult, ev.TurnEnd, ev.Question, ev.PermissionRequest, ev.ApiRetry)):
            self._last_progress_at = self._clock()
```

In `_drain`, handle `ApiRetry` as a status-flip that is NOT forwarded — add a branch in the per-event loop BEFORE `self._note_progress(e)` / `self._out.put(e)`:

```python
                    if isinstance(e, ev.ApiRetry):
                        self._note_progress(e)
                        if not self._retrying:
                            self._retrying = True
                            await self._emit_status()
                        continue   # internal — do not forward to the client
```

Clear the flag when a real (non-retry) event resumes the turn — at the top of the existing `if isinstance(e, ev.TextDelta)` / `elif ... TurnEnd` handling, add right after `for e in self._tr.feed(msg):` and the RateLimit `continue`:

```python
                    if self._retrying and not isinstance(e, ev.RateLimit):
                        self._retrying = False
                        # status re-emits below via existing turn flow / turn end
```

Expose it in `_dash_signals`:

```python
        return StatusSignals(
            turn_active=self._turn_active,
            pending_permission=bool(perms),
            pending_question=bool(self._qreg.pending(self.session_id)),
            pending_plan=any(p.get("tool") == "ExitPlanMode" for p in perms),
            turns_completed=self._turns_completed,
            last_turn_outcome=self._last_turn_outcome,
            retrying=self._retrying,
        )
```

(Requires `StatusSignals.retrying` from Task 4 — give it a default of `False` there so order doesn't matter.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_ENABLE_XVFB CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher-deploy/.venv/bin/python -m pytest server/tests/test_driver_stall.py server/tests/ -k 'driver or stall' -v`
Expected: PASS, including any pre-existing stall-watchdog tests (the empty-thinking-delta wedged-turn guard must still interrupt).

- [ ] **Step 5: Commit**

```bash
git add server/sessions/driver.py server/tests/test_driver_stall.py
git commit -m "fix(watchdog): count ApiRetry as progress so 529 backoff isn't interrupted

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: `retrying` status derivation

**Files:**
- Modify: `server/sessions/status.py` (`StatusSignals`, `derive_status`)
- Test: `server/tests/test_status.py` (add cases; file already exists)

**Interfaces:**
- Produces: `derive_status(StatusSignals(turn_active=True, retrying=True, ...)) == "retrying"`; precedence below the pending-permission/question/plan overlays, above plain `in_progress`.

- [ ] **Step 1: Write the failing test**

Add to `server/tests/test_status.py`:

```python
from server.sessions.status import StatusSignals, derive_status

def _sig(**kw):
    base = dict(turn_active=False, pending_permission=False, pending_question=False,
                pending_plan=False, turns_completed=0, last_turn_outcome="success")
    base.update(kw)
    return StatusSignals(**base)

def test_retrying_when_active_and_retrying():
    assert derive_status(_sig(turn_active=True, retrying=True)) == "retrying"

def test_active_without_retrying_is_in_progress():
    assert derive_status(_sig(turn_active=True)) == "in_progress"

def test_pending_question_outranks_retrying():
    assert derive_status(_sig(turn_active=True, retrying=True, pending_question=True)) == "waiting_question"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_ENABLE_XVFB CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher-deploy/.venv/bin/python -m pytest server/tests/test_status.py -v`
Expected: FAIL — `StatusSignals` has no `retrying`; `TypeError`.

- [ ] **Step 3: Implement**

In `server/sessions/status.py`, add the field to `StatusSignals` (with default so callers/tests don't all need updating):

```python
@dataclass(frozen=True)
class StatusSignals:
    turn_active: bool
    pending_permission: bool
    pending_question: bool
    pending_plan: bool
    turns_completed: int
    last_turn_outcome: str = "success"
    retrying: bool = False
```

In `derive_status`, add the `retrying` branch right after the `pending_permission` overlay and before `if sig.turn_active:`:

```python
    if sig.turn_active and sig.retrying:
        return "retrying"
    if sig.turn_active:
        return "in_progress"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_ENABLE_XVFB CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher-deploy/.venv/bin/python -m pytest server/tests/test_status.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/sessions/status.py server/tests/test_status.py
git commit -m "feat(status): derive 'retrying' status while an active turn backs off

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: Frontend `retrying` presentation

**Files:**
- Modify: `web/src/session/statusTier.ts` (`PRESENTATION`)
- Test: `web/src/session/statusTier.test.ts` (create if absent)

**Interfaces:**
- Consumes: status string `"retrying"` (Task 4, arrives via the existing `status` wire event).
- Produces: `statusPresentation("retrying")` → amber, label "Overloaded — retrying", tier `'busy'`.

- [ ] **Step 1: Write the failing test**

Create/extend `web/src/session/statusTier.test.ts`:

```ts
import { describe, it, expect } from 'vitest'
import { statusPresentation, statusTier } from './statusTier'

describe('retrying presentation', () => {
  it('is amber, busy tier, overloaded label', () => {
    const p = statusPresentation('retrying')
    expect(p.label).toBe('Overloaded — retrying')
    expect(p.tier).toBe('busy')
    expect(p.textClass).toContain('amber')
  })
  it('unknown status still falls back to Ready', () => {
    expect(statusTier('nonsense')).toBe('your-turn')
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && ./node_modules/.bin/vitest run src/session/statusTier.test.ts`
Expected: FAIL — `statusPresentation('retrying')` returns the `FALLBACK` (label "Ready").

- [ ] **Step 3: Implement**

In `web/src/session/statusTier.ts`, add to the `PRESENTATION` table (after the `in_progress`/`working` rows):

```ts
  retrying:         { tier: 'busy',      label: 'Overloaded — retrying', Icon: Loader2,       textClass: NEEDS_YOU },
```

(`NEEDS_YOU` is the amber class already defined in this file; `Loader2` is already imported. Tier `'busy'` keeps it sorting with active work, not in the error/needs-you groups.)

- [ ] **Step 4: Run test + typecheck to verify they pass**

Run: `cd web && ./node_modules/.bin/vitest run src/session/statusTier.test.ts && ./node_modules/.bin/tsc -b --force`
Expected: PASS, no type errors.

- [ ] **Step 5: Commit**

```bash
git add web/src/session/statusTier.ts web/src/session/statusTier.test.ts
git commit -m "feat(web): amber 'Overloaded — retrying' presentation for retrying status

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Final verification (after all tasks)

- [ ] Full backend suite: `env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_ENABLE_XVFB CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher-deploy/.venv/bin/python -m pytest server/tests/ -q` (expect the 2 known-environmental failures from memory: `remote_mode_wiring`, `x11` — unrelated to this change).
- [ ] Web: `cd web && ./node_modules/.bin/vitest run && ./node_modules/.bin/tsc -b --force && ./node_modules/.bin/eslint src`
- [ ] Open a non-draft PR, arm auto-merge: `gh pr create` (Claude Code trailer in body) then `gh pr merge <#> --squash --auto`.

## Self-Review notes

- **Spec coverage:** Change 1 (watchdog) → Tasks 2+3; Change 2 (classify overload) → Task 1 + Task 2 terminal copy; Change 3a (live "retrying") → Tasks 3+4+5; Change 3b (honest terminal) → Task 2 Step 3c. All spec sections mapped.
- **Refinement vs spec:** the spec said mid-turn retries "get the same `api_overloaded` code"; planning refined this to a dedicated **internal `ApiRetry`** event (avoids 20 transcript error-cards and any new frontend wire type — the live state rides the existing `status` event). Same behavior, smaller blast radius.
- **Type consistency:** `ApiRetry(turn_id, status, attempt, max_retries)` is referenced identically in Tasks 2/3; `StatusSignals.retrying` default `False` lets Tasks 3 and 4 land in either order.
