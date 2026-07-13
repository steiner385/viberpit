# Autopilot Cooldown (Human Deference) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** While a user is actively steering a live session, autopilot stands down for *that session only* until the user has been idle for `human_defer_seconds` (default 120), surfaced as a countdown badge.

**Architecture:** Wire up the already-declared-but-unconsumed `human_defer_seconds` autopilot config field. A wall-clock `last_user_interaction_at` timestamp is stamped on the `SessionRecord` at the WebSocket handler layer (user-originated only, so autopilot's own `SYSTEM_IDENTITY` actions never reset it). `_autopilot_get_context()` converts that to an elapsed `seconds_since_human` float that `pre_decide()` compares against the threshold as its first rule. The REST session serializer derives an `autopilotCooldownUntil` wire field the frontend counts down from locally.

**Tech Stack:** Python 3 / FastAPI / dataclasses / pytest (backend) · React + TypeScript + Vitest (frontend).

## Global Constraints

- **No new config field.** Reuse the existing `human_defer_seconds` (default 120) in
  `server/automations/autopilot.py`. Two cooldown knobs for one concept is a forbidden parallel
  implementation.
- **`last_user_interaction_at` is in-memory / process-local only.** Do NOT add it to
  `SessionRecord.to_dict`/`from_dict` (the RPC serde) and do NOT add a DB column — the window is
  120s and carries no meaning across a restart.
- **Stamp only at the ws.py handler layer**, never inside `orchestrator.send_prompt` /
  `resolve_permission` / `answer_question` (autopilot calls those directly with `SYSTEM_IDENTITY`;
  stamping there would let autopilot reset its own cooldown).
- **Autopilot's clock is `time.monotonic`.** Never compare a wall-clock timestamp against
  `pre_decide`'s `now`. Pass a precomputed `seconds_since_human` float into `DecisionContext`.
- Naive-UTC datetimes via `datetime.utcnow()`, matching `created_at` / `last_turn_at`; serialize
  with `server.time_iso.iso_utc`.
- Verify locally before each commit: `ruff`, `mypy server/ --ignore-missing-imports`, `pytest`
  (backend); `tsc -b` / `npm run build`, `vitest run`, `eslint` (web, run in `web/`).

---

### Task 1: Human-deference rule in `pre_decide` (pure policy)

**Files:**
- Modify: `server/automations/autopilot_policy.py` (`DecisionContext` dataclass ~L34-52; `pre_decide` ~L118-145)
- Test: `server/tests/test_autopilot_policy_rules.py`

**Interfaces:**
- Produces: `DecisionContext.seconds_since_human: float | None` (elapsed wall-seconds since the user
  last touched the session; `None` = never / unknown). `pre_decide(ctx, config, now)` returns
  `Decision(action="skip", reason="human steering — deferring")` when
  `ctx.seconds_since_human is not None and ctx.seconds_since_human < int(config.get("human_defer_seconds", 120))`.

- [ ] **Step 1: Write the failing tests**

Add to `server/tests/test_autopilot_policy_rules.py` (follow the file's existing `DecisionContext`
construction helper if present; otherwise construct directly with the required positional fields
`session_id, status, account, repo, branch, effective_goal, goal_source, stuck`):

```python
from server.automations.autopilot_policy import DecisionContext, pre_decide

def _ctx(**kw):
    base = dict(session_id="s1", status="idle", account="a", repo=None, branch=None,
                effective_goal="g", goal_source="title", stuck={"kind": "idle"})
    base.update(kw)
    return DecisionContext(**base)

def test_defers_while_human_recently_active():
    ctx = _ctx(seconds_since_human=10.0)
    d = pre_decide(ctx, {"human_defer_seconds": 120, "max_actions_per_session_per_hour": 12,
                         "loop_repeat_limit": 3, "recovery_max_attempts": 2}, now=1000.0)
    assert d is not None and d.action == "skip" and "human" in d.reason

def test_acts_after_defer_window_elapsed():
    ctx = _ctx(seconds_since_human=130.0)
    d = pre_decide(ctx, {"human_defer_seconds": 120, "max_actions_per_session_per_hour": 12,
                         "loop_repeat_limit": 3, "recovery_max_attempts": 2}, now=1000.0)
    assert d is None  # falls through to the LLM decider

def test_no_deference_when_human_never_touched():
    ctx = _ctx(seconds_since_human=None)
    d = pre_decide(ctx, {"human_defer_seconds": 120, "max_actions_per_session_per_hour": 12,
                         "loop_repeat_limit": 3, "recovery_max_attempts": 2}, now=1000.0)
    assert d is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest server/tests/test_autopilot_policy_rules.py -k human -v`
Expected: FAIL — `DecisionContext` has no `seconds_since_human` (TypeError) / rule missing.

- [ ] **Step 3: Add the field**

In `server/automations/autopilot_policy.py`, add to the `DecisionContext` dataclass (with the other
defaulted fields, after `reversal_hint`):

```python
    # Elapsed wall-seconds since the user last steered this session (prompt/answer/permission/typing).
    # Precomputed by _autopilot_get_context (autopilot's clock is monotonic, so a raw timestamp
    # cannot be compared against pre_decide's `now`). None = the human never touched it.
    seconds_since_human: float | None = None
```

- [ ] **Step 4: Add the rule as the FIRST check in `pre_decide`**

Insert at the very top of `pre_decide`'s body, before the deny-list block (the missing precedence
rule 1):

```python
    # 1. Human deference — stand down while the human is actively steering this session.
    if ctx.seconds_since_human is not None and ctx.seconds_since_human < int(config.get("human_defer_seconds", 120)):
        return Decision(action="skip", reason="human steering — deferring")
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest server/tests/test_autopilot_policy_rules.py -v`
Expected: PASS (all, including pre-existing rules).

- [ ] **Step 6: Commit**

```bash
git add server/automations/autopilot_policy.py server/tests/test_autopilot_policy_rules.py
git commit -m "feat(autopilot): human-deference rule in pre_decide (wires human_defer_seconds)"
```

---

### Task 2: Stamp `last_user_interaction_at` + feed `seconds_since_human`

**Files:**
- Modify: `server/sessions/types.py` (`SessionRecord` dataclass, add field ~after L56)
- Modify: `server/sessions/orchestrator.py` (`_autopilot_get_context` ~L1787-1822)
- Test: `server/tests/test_autopilot_orchestrator_seams.py`

**Interfaces:**
- Produces: `SessionRecord.last_user_interaction_at: datetime | None = None`.
  `_autopilot_get_context(session_id)` sets `DecisionContext.seconds_since_human` to
  `(datetime.utcnow() - rec.last_user_interaction_at).total_seconds()` when the field is set, else
  `None`.
- Consumes: `DecisionContext.seconds_since_human` from Task 1.

- [ ] **Step 1: Write the failing test**

Add to `server/tests/test_autopilot_orchestrator_seams.py` (use the module's existing orchestrator
fixture/builder; the assertion is what matters):

```python
import datetime as _dt

async def test_get_context_reports_seconds_since_human(orchestrator_with_session):
    orch, sid = orchestrator_with_session
    rec = orch._records[sid]
    rec.last_user_interaction_at = _dt.datetime.utcnow() - _dt.timedelta(seconds=5)
    ctx = await orch._autopilot_get_context(sid)
    assert ctx is not None
    assert 4.0 <= ctx.seconds_since_human <= 30.0

async def test_get_context_none_when_never_touched(orchestrator_with_session):
    orch, sid = orchestrator_with_session
    orch._records[sid].last_user_interaction_at = None
    ctx = await orch._autopilot_get_context(sid)
    assert ctx.seconds_since_human is None
```

> If no reusable session fixture exists in this file, mirror the setup used by neighboring tests in
> `test_autopilot_orchestrator_seams.py` to construct an orchestrator with one record.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest server/tests/test_autopilot_orchestrator_seams.py -k seconds_since_human -v`
Expected: FAIL — `SessionRecord` has no `last_user_interaction_at` / `ctx.seconds_since_human` unset.

- [ ] **Step 3: Add the record field**

In `server/sessions/types.py`, after `auto_archive_opt_out_reason` (~L56):

```python
    # Wall-clock time the user last steered this session (prompt/answer/permission/typing ping).
    # Drives autopilot human-deference + the cooldown badge. In-memory/process-local ONLY: NOT in
    # to_dict/from_dict, NOT a DB column — a 120s window is meaningless across a restart.
    last_user_interaction_at: datetime | None = None
```

- [ ] **Step 4: Compute `seconds_since_human` in `_autopilot_get_context`**

In `server/sessions/orchestrator.py`, inside `_autopilot_get_context`, before the
`return DecisionContext(...)` (~L1815), add:

```python
        from datetime import datetime as _dtmod
        _last = getattr(rec, "last_user_interaction_at", None)
        _secs_since_human = (_dtmod.utcnow() - _last).total_seconds() if _last is not None else None
```

and add to the `DecisionContext(...)` constructor call:

```python
            seconds_since_human=_secs_since_human,
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest server/tests/test_autopilot_orchestrator_seams.py -k seconds_since_human -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add server/sessions/types.py server/sessions/orchestrator.py server/tests/test_autopilot_orchestrator_seams.py
git commit -m "feat(autopilot): track last_user_interaction_at; feed seconds_since_human to context"
```

---

### Task 3: WebSocket handlers stamp on user actions + new `activity` message

**Files:**
- Modify: `server/bridge/ws.py` (inbound dispatch ~L899-957: `prompt`, `permissionResponse`, `answerQuestion`; add `activity` branch)
- Test: `server/tests/test_ws.py` (or `test_ws_commands.py` — match where inbound-message tests already live)

**Interfaces:**
- Consumes: `SessionRecord.last_user_interaction_at` (Task 2).
- Produces: after any of `prompt` / `permissionResponse` / `answerQuestion` / `activity` inbound
  messages, `orchestrator._records[session_id].last_user_interaction_at` is set to ~now. Autopilot's
  own `_autopilot_apply` (which calls the orchestrator methods with `SYSTEM_IDENTITY`, not via ws.py)
  does NOT set it.

- [ ] **Step 1: Write the failing tests**

Add to `server/tests/test_ws.py` (reuse the file's existing WS-client / connect fixture and the
established pattern for sending an inbound frame; the two behaviors to assert):

```python
# 1. Each user-originated message stamps last_user_interaction_at.
async def test_activity_message_stamps_interaction(ws_connected_session):
    ws, orch, sid = ws_connected_session
    assert orch._records[sid].last_user_interaction_at is None
    await ws.send_json({"type": "activity", "sessionId": sid})
    await _drain(ws)  # let the server task process the frame (use the file's existing helper)
    assert orch._records[sid].last_user_interaction_at is not None

# 2. Anti-self-reset: autopilot's apply path must NOT stamp it.
async def test_autopilot_apply_does_not_stamp_interaction(orchestrator_with_live_session):
    orch, sid = orchestrator_with_live_session
    orch._records[sid].last_user_interaction_at = None
    from server.automations.autopilot_policy import Decision
    await orch._autopilot_apply(sid, Decision(action="nudge", prompt="keep going"), None)
    assert orch._records[sid].last_user_interaction_at is None
```

> Use whatever connect/live-session fixtures the neighboring tests in `test_ws.py` /
> `test_autopilot_orchestrator_seams.py` already provide; match their `send_json` / drain idiom
> exactly (see the WS-frame invariant note — every session frame carries `sessionId`).

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest server/tests/test_ws.py -k "activity_message_stamps or apply_does_not_stamp" -v`
Expected: FAIL — no `activity` handler; timestamp stays `None` on the stamp test.

- [ ] **Step 3: Add a stamp helper + call it in the three existing handlers**

In `server/bridge/ws.py`, add a small local helper near the dispatch (module-level import
`from datetime import datetime` if not already present), and call it inside the `prompt`,
`permissionResponse`, and `answerQuestion` branches (right after `session_id` is known, before/after
the orchestrator call is fine):

```python
def _stamp_user_interaction(session_id: str) -> None:
    rec = orchestrator._records.get(session_id)
    if rec is not None:
        rec.last_user_interaction_at = datetime.utcnow()
```

Add `_stamp_user_interaction(session_id)` in each of the `prompt`, `permissionResponse`, and
`answerQuestion` branches.

> If `orchestrator` in ws.py is a module-level singleton, reference it as the existing handlers do
> (they already call `orchestrator.send_prompt(...)` etc.). Keep the helper's access identical to
> those calls.

- [ ] **Step 4: Add the `activity` branch**

Insert after the `answerQuestion` branch (before `cancelQueued`, ~L957):

```python
        elif msg_type == "activity":
            # Lightweight steering signal — user typing / interacting before send. Refreshes the
            # human-deference window; no response frame, no orchestrator call.
            _stamp_user_interaction(session_id)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest server/tests/test_ws.py -k "activity_message_stamps or apply_does_not_stamp" -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add server/bridge/ws.py server/tests/test_ws.py
git commit -m "feat(ws): stamp last_user_interaction_at on user actions; add activity message"
```

---

### Task 4: Serialize `autopilotCooldownUntil` in the REST session payload

**Files:**
- Modify: `server/api/sessions.py` (`_record_to_dict` ~L96-155; `list_sessions` ~L228-264)
- Test: `server/tests/test_sessions_payload.py`

**Interfaces:**
- Consumes: `SessionRecord.last_user_interaction_at` (Task 2); `human_defer_seconds` via
  `orch._autopilot_read_config()` (returns `(mode, config)`).
- Produces: wire field `autopilotCooldownUntil: string | null` — an ISO instant in the future while
  the session is within the deference window (only when autopilot mode != "off" and the session is
  not `autopilot_paused`), else absent/`None`. `_record_to_dict` gains `defer_seconds: int | None = None`.

- [ ] **Step 1: Write the failing test**

Add to `server/tests/test_sessions_payload.py` (this test targets `_record_to_dict` directly — no
HTTP needed):

```python
import datetime as _dt
from server.api.sessions import _record_to_dict

def _rec(**kw):
    # Build a minimal SessionRecord the way neighboring tests in this file do; set the fields used.
    from server.sessions.types import SessionRecord
    r = SessionRecord(id="s1", name="n", account="a", claude_session_id="c", cols=80, rows=24, cwd="/tmp")
    for k, v in kw.items():
        setattr(r, k, v)
    return r

def test_cooldown_until_present_within_window():
    rec = _rec(last_user_interaction_at=_dt.datetime.utcnow())
    out = _record_to_dict(rec, defer_seconds=120)
    assert out.get("autopilotCooldownUntil") is not None

def test_cooldown_until_absent_after_window():
    rec = _rec(last_user_interaction_at=_dt.datetime.utcnow() - _dt.timedelta(seconds=200))
    out = _record_to_dict(rec, defer_seconds=120)
    assert out.get("autopilotCooldownUntil") in (None, )

def test_cooldown_until_absent_when_paused():
    rec = _rec(last_user_interaction_at=_dt.datetime.utcnow(), autopilot_paused=True)
    out = _record_to_dict(rec, defer_seconds=120)
    assert out.get("autopilotCooldownUntil") in (None, )

def test_cooldown_until_absent_when_defer_none():
    rec = _rec(last_user_interaction_at=_dt.datetime.utcnow())
    out = _record_to_dict(rec, defer_seconds=None)
    assert out.get("autopilotCooldownUntil") in (None, )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest server/tests/test_sessions_payload.py -k cooldown -v`
Expected: FAIL — `_record_to_dict` takes no `defer_seconds` / no `autopilotCooldownUntil` key.

- [ ] **Step 3: Add the param + derived field to `_record_to_dict`**

Change the signature (add the new keyword at the end):

```python
def _record_to_dict(
    record, repo: Any = _UNSET, suggestions: list[str] | None = None,
    prs: list | None = None, summary: str = "", defer_seconds: int | None = None,
) -> dict[str, Any]:
```

Before the `return {...}` (after `goal_text, goal_source = effective_goal(record)`), compute:

```python
    from datetime import datetime, timedelta
    cooldown_until = None
    _last = getattr(record, "last_user_interaction_at", None)
    if defer_seconds and _last is not None and not getattr(record, "autopilot_paused", False):
        _until = _last + timedelta(seconds=defer_seconds)
        if _until > datetime.utcnow():
            cooldown_until = iso_utc(_until)
```

Add to the returned dict (near `autopilotPaused`):

```python
        "autopilotCooldownUntil": cooldown_until,
```

- [ ] **Step 4: Pass the live config from `list_sessions`**

In `list_sessions` (~L228-264), before the loop that builds records, read the autopilot config once
and thread it through the `_record_to_dict` call at ~L264:

```python
    try:
        _mode, _ap_cfg = await orch._autopilot_read_config()
        _defer = int(_ap_cfg.get("human_defer_seconds", 120)) if _mode != "off" else None
    except Exception:
        _defer = None
```

```python
        _record_to_dict(r, repo=repo, suggestions=s, prs=pr_list, summary=summary, defer_seconds=_defer)
```

> Confirm the orchestrator handle name in this module (`orch`); use whatever the surrounding code
> already uses to reach the orchestrator. Other `_record_to_dict` callers keep the `None` default
> (single-session endpoints don't drive the dashboard badge).

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest server/tests/test_sessions_payload.py -k cooldown -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add server/api/sessions.py server/tests/test_sessions_payload.py
git commit -m "feat(api): emit autopilotCooldownUntil in session payload (human-deference badge)"
```

---

### Task 5: Frontend — `Session` type + throttled `activity()` WS client method

**Files:**
- Modify: `web/src/api/types.ts` (`Session` interface ~L60-113)
- Modify: `web/src/ws/client.ts` (add `activity()` ~near `permissionResponse`/`answerQuestion`, L223-233; uses the `_sendControl` helper)
- Test: `web/src/ws/client.control.test.ts`

**Interfaces:**
- Produces: `Session.autopilotCooldownUntil?: string | null`. `SessionSocket.activity(sessionId: string): void`
  — sends `{type:'activity', sessionId}` at most once per `ACTIVITY_THROTTLE_MS` (5000) per socket;
  calls within the window are dropped.

- [ ] **Step 1: Write the failing test**

Add to `web/src/ws/client.control.test.ts` (reuse the file's existing socket harness /
mock-WebSocket setup and its fake-timer pattern):

```ts
it('throttles activity() to at most one send per window', () => {
  vi.useFakeTimers()
  const { socket, sent } = makeOpenSocket()          // use this file's existing helper
  socket.activity('s1')
  socket.activity('s1')                               // dropped (within window)
  expect(sent.filter(m => m.type === 'activity')).toHaveLength(1)
  vi.advanceTimersByTime(5001)
  socket.activity('s1')                               // allowed again
  expect(sent.filter(m => m.type === 'activity')).toHaveLength(2)
  vi.useRealTimers()
})
```

> If `client.control.test.ts` has no `makeOpenSocket`/`sent` helper, mirror the exact
> socket-construction + send-capture used by the sibling tests in that file.

- [ ] **Step 2: Run test to verify it fails**

Run (in `web/`): `npx vitest run src/ws/client.control.test.ts -t activity`
Expected: FAIL — `socket.activity` is not a function.

- [ ] **Step 3: Add the type field**

In `web/src/api/types.ts`, in the `Session` interface (near `autopilotPaused`):

```ts
  /** ISO instant until which autopilot is deferring because the user is steering; null/absent = not deferring. */
  autopilotCooldownUntil?: string | null
```

- [ ] **Step 4: Add the throttled method**

In `web/src/ws/client.ts`, add a module constant and a method on the socket class (alongside
`permissionResponse`/`answerQuestion`, ~L223):

```ts
const ACTIVITY_THROTTLE_MS = 5000
```

```ts
  private _lastActivitySent = 0
  activity(sessionId: string): void {
    const now = Date.now()
    if (now - this._lastActivitySent < ACTIVITY_THROTTLE_MS) return
    this._lastActivitySent = now
    this._sendControl({ type: 'activity', sessionId })
  }
```

> `_sendControl` is best-effort (returns false if the socket is closed) — matching
> `permissionResponse`/`answerQuestion`; a dropped activity ping is harmless.

- [ ] **Step 5: Run test to verify it passes**

Run (in `web/`): `npx vitest run src/ws/client.control.test.ts -t activity`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add web/src/api/types.ts web/src/ws/client.ts web/src/ws/client.control.test.ts
git commit -m "feat(web): Session.autopilotCooldownUntil + throttled activity() ws method"
```

---

### Task 6: Frontend — emit `activity` on typing / question-permission interaction

**Files:**
- Modify: `web/src/session/useSessionRuntime.ts` (the hook that already calls `prompt`/`permissionResponse`/`answerQuestion`, ~L175-255) — expose an `onActivity()` callback, OR the composer input component that owns the textarea `onChange`.
- Test: `web/src/session/` — add `useSessionRuntime.activity.test.ts` (or extend an existing runtime test), matching the directory's testing style.

**Interfaces:**
- Consumes: `SessionSocket.activity(sessionId)` (Task 5).
- Produces: a stable `onActivity()` handler wired to the composer's `onChange` (typing) and to the
  question/permission widget interaction, calling `client.activity(sessionId)` (throttling lives in
  the client, so callers may fire freely).

- [ ] **Step 1: Write the failing test**

Add a focused test asserting the wiring calls through. Mirror how existing runtime tests mock the
ws client:

```ts
it('emits activity on composer typing', () => {
  const activity = vi.fn()
  const rt = renderRuntime({ client: { ...mockClient, activity } })   // use the file's harness
  rt.onActivity()                                                     // the handler wired to onChange
  expect(activity).toHaveBeenCalledWith(expect.any(String))          // sessionId
})
```

> Match the actual hook API. If the runtime hook returns handlers, assert on the returned
> `onActivity`; if activity is wired inside the composer component instead, write the test at that
> component's level (fire a `change` event on the textarea, assert `client.activity` was called).

- [ ] **Step 2: Run test to verify it fails**

Run (in `web/`): `npx vitest run src/session -t activity`
Expected: FAIL — no `onActivity` wiring yet.

- [ ] **Step 3: Wire the emitter**

In `web/src/session/useSessionRuntime.ts`, add a memoized handler next to the existing
prompt/permission/question handlers:

```ts
  const onActivity = useCallback(() => {
    client.activity(sessionId)
  }, [client, sessionId])
```

Return `onActivity` from the hook (add to its return object), and call it:
- from the composer textarea `onChange` (typing), and
- from the question/permission widget interaction (e.g. option select / focus).

> Follow the existing return-shape + call-site conventions in this hook and the composer. Keep the
> handler dependency-stable (`useCallback`) so the textarea doesn't re-bind every render.

- [ ] **Step 4: Run test to verify it passes**

Run (in `web/`): `npx vitest run src/session -t activity`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add web/src/session/
git commit -m "feat(web): emit activity ping on composer typing + question/permission interaction"
```

---

### Task 7: Frontend — SessionCard cooldown badge

**Files:**
- Modify: `web/src/components/SessionCard.tsx` (badge footer — the `card-badges` group that holds `autopilotPaused`/secondary badges, ~L91+)
- Test: `web/src/components/SessionCard.cooldown.test.tsx` (new)

**Interfaces:**
- Consumes: `Session.autopilotCooldownUntil` (Task 5).
- Produces: a **non-interactive** badge rendered iff `autopilotCooldownUntil` is a future instant,
  showing e.g. `Autopilot paused — you're steering (1m left)`, updating once per second and
  disappearing when the instant passes. Lives in the `card-badges` footer (sibling of the open
  button), never inside the open-button (nested-interactive a11y).

- [ ] **Step 1: Write the failing test**

Create `web/src/components/SessionCard.cooldown.test.tsx` (mirror imports/render harness from
`SessionCard.summary.test.tsx`):

```tsx
import { render, screen } from '@testing-library/react'
import { SessionCard } from './SessionCard'
// build a minimal Session via the test factory used in SessionCard.summary.test.tsx

it('shows the cooldown badge while autopilotCooldownUntil is in the future', () => {
  const future = new Date(Date.now() + 90_000).toISOString()
  render(<SessionCard session={makeSession({ autopilotCooldownUntil: future })} /* ...required props */ />)
  expect(screen.getByText(/you're steering/i)).toBeInTheDocument()
})

it('hides the cooldown badge when autopilotCooldownUntil is absent', () => {
  render(<SessionCard session={makeSession({ autopilotCooldownUntil: null })} /* ...required props */ />)
  expect(screen.queryByText(/you're steering/i)).not.toBeInTheDocument()
})
```

> Reuse the exact `makeSession` / required-prop setup from `SessionCard.summary.test.tsx` so the
> card mounts (jsdom offset stub etc. are already handled by `web/src/setup.ts`).

- [ ] **Step 2: Run test to verify it fails**

Run (in `web/`): `npx vitest run src/components/SessionCard.cooldown.test.tsx`
Expected: FAIL — no badge.

- [ ] **Step 3: Implement the badge**

In `SessionCard.tsx`, inside the `card-badges` footer group, add a small local countdown. Use a
1s-interval `useState`/`useEffect` (or an existing "now" hook if the file already has one) to
recompute the remaining time and hide the badge once it passes:

```tsx
const cooldownUntil = session.autopilotCooldownUntil ? new Date(session.autopilotCooldownUntil).getTime() : null
const [now, setNow] = useState(() => Date.now())
useEffect(() => {
  if (!cooldownUntil || cooldownUntil <= Date.now()) return
  const id = setInterval(() => setNow(Date.now()), 1000)
  return () => clearInterval(id)
}, [cooldownUntil])
const cooldownLeftMs = cooldownUntil ? cooldownUntil - now : 0
// render iff cooldownLeftMs > 0:
{cooldownLeftMs > 0 && (
  <span className="...badge classes matching sibling badges...">
    Autopilot paused — you're steering ({Math.ceil(cooldownLeftMs / 60000)}m left)
  </span>
)}
```

> Match the badge styling/classes of the neighboring secondary badges. Keep it a plain `<span>` (no
> button/link) so it stays non-interactive. Format helper: for < 60s you may show `(<1m left)` —
> keep copy consistent with the test's `/you're steering/i` matcher.

- [ ] **Step 4: Run test to verify it passes**

Run (in `web/`): `npx vitest run src/components/SessionCard.cooldown.test.tsx`
Expected: PASS

- [ ] **Step 5: Full web verification**

Run (in `web/`): `npm run build && npx vitest run && npx eslint src`
Expected: type-check + all tests + lint clean.

- [ ] **Step 6: Commit**

```bash
git add web/src/components/SessionCard.tsx web/src/components/SessionCard.cooldown.test.tsx
git commit -m "feat(web): SessionCard cooldown badge while user is steering"
```

---

## Final verification (before PR)

- [ ] Backend: `ruff check server/ && python -m mypy server/ --ignore-missing-imports && python -m pytest server/tests/ -k "autopilot or ws or sessions_payload"`
- [ ] Web (in `web/`): `npm run build && npx vitest run && npx eslint src`
- [ ] Push branch `feat/autopilot-user-cooldown`; open a **non-draft** PR with `gh pr create`
  (body ends with the Claude Code trailer); `gh pr ready` if needed; `gh pr merge <#> --squash --auto`.

## Self-Review (author checklist — completed)

- **Spec coverage:** per-session scope (Task 1 gate keys on the single session's `seconds_since_human`);
  triggers = sent input + typing (Tasks 3, 6); duration 120s via `human_defer_seconds` (Task 1);
  badge (Tasks 4, 7). ✔
- **Placeholders:** none — every step carries concrete code; fixture-name flexibility is explicitly
  bounded ("mirror the neighboring test"), not left as TODO. ✔
- **Type consistency:** `seconds_since_human` (float|None), `last_user_interaction_at` (datetime|None),
  `defer_seconds` (int|None), `autopilotCooldownUntil` (string|null), `activity(sessionId)` — names
  identical across producing/consuming tasks. ✔
