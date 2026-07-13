# Autopilot Cooldown While the User Is Steering — Design

**Date:** 2026-07-12
**Status:** Approved (design), pending implementation
**Feature:** Per-session, auto-expiring autopilot stand-down triggered by live user interaction.

## Problem

When a user is actively steering a live `claude` session, autopilot can inject its own
autonomous direction (a prompt, an answer, a permission decision) into that same session —
producing conflicting instructions racing against the human. There is currently no notion of
"the human is driving this session right now, stand down."

## Goal

Give each session a transient "cool down" window: while the user is interacting with a session,
autopilot skips **that session only** and resumes automatically once the user has been inactive
for a configurable grace period (default 120s). All other sessions continue to be driven normally.

## Decisions (locked)

| Question | Decision |
|----------|----------|
| Scope | **Per-session** — only the session being steered stands down; all other sessions keep running. |
| Which automations | **Only autopilot's in-session steering.** PR Doctor / backlog / consolidate are untouched. |
| Trigger | **Sent input** (prompt / answer / permission) **and** **typing/answer-selection activity** detected in the browser (protects composition *before* send). |
| Duration | **120s**, via the **existing** `human_defer_seconds` autopilot config field (default already 120). No new config field. |
| Visibility | **UI badge** on the session ("Autopilot paused — you're steering (Nm left)"). |

### Key finding: this feature finishes an unwired config field

Autopilot's config schema already declares `human_defer_seconds` (default 120,
help: *"Skip a session a human touched this recently."*) but **nothing in the codebase
consumes it** — it is dead/aspirational config. The numbered precedence comments in
`pre_decide` (`autopilot_policy.py`) run 2, 5, 7, 8 — the missing low numbers imply a
documented "human deference" rule (≈ rule 1) that was never implemented. Per the project's
"find the existing source of truth" / "no dead code" norms, this feature **wires up
`human_defer_seconds`** rather than adding a parallel `user_interaction_cooldown_seconds`.
Two cooldown knobs for one concept is the exact anti-pattern to avoid.

## Non-Goals (YAGNI)

- No global / cross-session pause (interacting with session A does not pause session B).
- No pausing of PR Doctor, backlog runner, or consolidate/spawn automations.
- No DB persistence of the cooldown timestamp (it is transient; stale after any restart).
- No server-side per-second countdown push — the frontend counts down locally from a supplied
  `until` timestamp.

## Architecture

### State (in-memory only — no DB column, no migration)

Add one field to `SessionRecord` (`server/sessions/types.py`):

```python
last_user_interaction_at: datetime | None = None  # naive UTC; stamped on genuine user actions
```

Rationale for in-memory only: a 120s cooldown carries no meaning across a process restart, so
persisting it to SQLite would only store stale data and force a migration for zero benefit. It
lives on the orchestrator's in-memory `_records[session_id]` alongside other transient state.

Derived value exposed to the frontend in the session status DTO:

```python
autopilot_cooldown_until: datetime | None
# = last_user_interaction_at + cooldown_seconds, ONLY when that instant is in the future; else None
```

### Trigger points — stamp `last_user_interaction_at = now()`

All stamping happens at the **WebSocket handler layer** in `server/bridge/ws.py`, on
**user-originated** messages only:

1. `prompt` message — user sends a prompt.
2. `permissionResponse` — user approves/denies a permission.
3. `answerQuestion` — user answers a question.
4. **new** `activity` message — a throttled ping (~1 per 5s) the frontend emits while the user is
   typing in the prompt box or interacting with a question/permission widget, so the cooldown
   protects the user *while still composing*, before they hit send.

**Critical constraint — anti-self-reset:** stamping MUST NOT live inside the shared orchestrator
methods (`send_prompt` / `resolve_permission` / `resolve_question`), because autopilot's own
`apply()` routes its injected actions through those same methods. Stamping there would let
autopilot reset its *own* cooldown on every action, defeating the gate. The WS-handler layer is
user-originated by construction, which is why the stamp lives there. (Implementation must verify
that `apply()` indeed routes through the shared orchestrator methods and confirm no other
user-facing entrypoint bypasses the WS layer.)

### The `activity` WebSocket message

- **Client → server:** `{"type": "activity", "sessionId": "<id>"}` (must carry `sessionId` per the
  established WS-frame invariant).
- **Frontend emission:** throttled to at most one message per ~5s while the user is actively typing
  in the prompt input or interacting with a question/permission widget. Throttle interval is a
  frontend constant (starting value 5s).
- **Server handling:** stamp `rec.last_user_interaction_at = now()`. No response frame required.

### Config

**No new config field.** Reuse the existing `human_defer_seconds` (default 120) already in the
autopilot config schema (`server/automations/autopilot.py`). This feature is what finally consumes
it (see "Key finding" above).

### The gate — and why it uses precomputed elapsed, not a timestamp

Autopilot's control loop injects `clock=time.monotonic` (`orchestrator.py`), so `now` inside
`pre_decide` and the ledger's `hour_actions` are **monotonic**, not wall-clock. But
`last_user_interaction_at` must be **wall-clock** (to serialize as the badge `until` and to survive
as a human-readable time). Comparing a wall-clock timestamp against a monotonic `now` would be a
bug. The fix: `_autopilot_get_context()` (`server/sessions/orchestrator.py`) computes the elapsed
wall time once and passes it into the context as `seconds_since_human`, so `pre_decide` compares a
plain float against the threshold — no clock coupling, pure, trivially unit-testable.

`DecisionContext` gains `seconds_since_human: float | None = None`.

In `pre_decide()` (`server/automations/autopilot_policy.py`), as the **first** rule — ahead of
deny-list, hourly budget, and loop detection — so a steering human is never blocked on a lower gate
(this is the missing precedence rule the numbered comments imply):

```python
# 1. Human deference — stand down while the human is actively steering this session.
defer = int(config.get("human_defer_seconds", 120))
if ctx.seconds_since_human is not None and ctx.seconds_since_human < defer:
    return Decision(action="skip", reason="human steering — deferring")
```

`_autopilot_get_context()` computes it:

```python
secs = None
if rec.last_user_interaction_at is not None:
    secs = (datetime.utcnow() - rec.last_user_interaction_at).total_seconds()
# ... DecisionContext(..., seconds_since_human=secs)
```

The skip is logged like any other autopilot skip (no new logging surface required).

### UI badge

The `SessionCard` reads the full record from the REST `/api/sessions` poll via `_record_to_dict()`
(`server/api/sessions.py`), **not** the WS `Status` event. So the derived `autopilotCooldownUntil`
is emitted there. `_record_to_dict` gains an optional `defer_seconds: int | None = None` param; the
`list_sessions` endpoint reads the autopilot config once (`orch._autopilot_read_config()`), and
passes `human_defer_seconds` only when autopilot mode is not "off" (else `None`). Per record:

```python
if defer_seconds and not record.autopilot_paused and record.last_user_interaction_at is not None:
    until = record.last_user_interaction_at + timedelta(seconds=defer_seconds)
    if until > datetime.utcnow():
        out["autopilotCooldownUntil"] = iso_utc(until)   # else omit / null
```

This gates the badge on autopilot actually being able to defer (mode on, session not paused), so it
never shows a misleading "paused" when autopilot wasn't going to act anyway. The card renders a
**non-interactive** badge such as **"Autopilot paused — you're steering (1m left)"**, counting down
locally and disappearing once the timestamp passes.

Placement follows the established SessionCard conventions:
- Lives in the wrapping `card-badges` footer (sibling of the open button), **not** inside the
  open-button (avoids the nested-interactive a11y issue).
- Visibility gated by the existing container-query breakpoints used for secondary badges.

## Data Flow

```
User interaction (WebSocket, user-originated)
  ├─ prompt            ─→ ws.py handler ─→ [stamp last_user_interaction_at] ─→ orchestrator.send_prompt()
  ├─ permissionResponse─→ ws.py handler ─→ [stamp] ─→ orchestrator.resolve_permission()
  ├─ answerQuestion    ─→ ws.py handler ─→ [stamp] ─→ orchestrator.resolve_question()
  └─ activity (new)    ─→ ws.py handler ─→ [stamp]        (no orchestrator call)

Session status broadcast
  └─ include autopilot_cooldown_until (derived) ─→ SessionCard badge (counts down locally)

Autopilot sweep (every ~20s)
  └─ AutopilotLoop.tick()
       └─ _autopilot_get_context()  [attach last_user_interaction_at to DecisionContext]
            └─ pre_decide()  ── FIRST GATE: in-cooldown? ─→ Decision(skip, "user steering")
                              └─ else fall through to deny-list / budget / loop / LLM decider
```

## Error Handling / Edge Cases

- **Clock/`None` safety:** a `None` `last_user_interaction_at` (never interacted) means no cooldown —
  autopilot acts normally.
- **Autopilot self-action:** guaranteed not to reset the timer because stamping is WS-layer-only
  (see anti-self-reset constraint).
- **Restart:** in-memory timestamp is lost on restart; acceptable — the window is 120s and any
  restart exceeds a meaningful steering session. Autopilot simply resumes.
- **Multiple viewers:** any user's genuine interaction stamps the single per-session timestamp; the
  most recent interaction wins. No per-user tracking needed.
- **`activity` flood:** bounded by the frontend ~5s throttle; server handling is O(1) (one
  assignment), so even an unthrottled client is cheap.

## Testing

**Backend — policy unit test** (`pre_decide`):
- Skips with reason "user steering …" when `now - last_user_interaction_at < cooldown`.
- Falls through (does not skip on this gate) when the window has elapsed.
- No cooldown effect when `last_user_interaction_at is None`.
- Deterministic via injected `now` and timestamp.

**Backend — WS handler test** (`server/tests/test_ws.py` family):
- Each of the four trigger messages (`prompt`, `permissionResponse`, `answerQuestion`, `activity`)
  stamps `last_user_interaction_at`.
- **Anti-self-reset guard:** an autopilot `apply()` action does NOT stamp
  `last_user_interaction_at`.

**Backend — status DTO test:**
- `autopilot_cooldown_until` is populated (future instant) right after an interaction and is `None`
  once the window has elapsed.

**Frontend — vitest:**
- Badge renders while `autopilot_cooldown_until` is in the future; disappears after it passes.
- Typing in the prompt input emits a throttled `activity` message (≤ 1 per throttle interval).

## Files Touched (anticipated)

- `server/sessions/types.py` — add `last_user_interaction_at: datetime | None` to `SessionRecord`.
  Not persisted to the DB (no column, no migration — a 120s window is meaningless across a restart),
  BUT it IS carried on the RPC wire via `to_dict`/`from_dict`. In daemon mode the edge process
  reconstructs records from the daemon over RPC (`RemoteOrchestrator.list_sessions` →
  `SessionRecord.from_dict`); the badge serializer (`_record_to_dict`) runs on the edge, so the field
  must survive the round trip or the badge is dead in production. NOTE: `iso_utc`→`fromisoformat`
  yields a tz-AWARE datetime on the edge, so any consumer comparing it against a naive `utcnow()`
  must normalize first (see `_record_to_dict`).
- `server/bridge/ws.py` — stamp `rec.last_user_interaction_at = datetime.utcnow()` on `prompt` /
  `permissionResponse` / `answerQuestion`; handle new `activity` message (stamp only).
- `server/automations/autopilot_policy.py` — add `seconds_since_human` to `DecisionContext`; add the
  first-rule human-deference check in `pre_decide()` reading the existing `human_defer_seconds`.
  (No change to `autopilot.py` — the config field already exists.)
- `server/sessions/orchestrator.py` — compute `seconds_since_human` from
  `rec.last_user_interaction_at` in `_autopilot_get_context()` and pass it into `DecisionContext`.
- `server/api/sessions.py` — `_record_to_dict` gains `defer_seconds` param + emits
  `autopilotCooldownUntil`; `list_sessions` reads autopilot config and passes it.
- `web/src/api/types.ts` — add `autopilotCooldownUntil?: string | null` to `Session`.
- `web/src/ws/client.ts` — add throttled `activity(sessionId)` method.
- `web/src/session/useSessionRuntime.ts` (+ composer input) — emit `activity` on typing /
  question-permission interaction.
- `web/src/components/SessionCard.tsx` — the countdown badge in the `card-badges` footer.
- Tests as listed above.

## Open Implementation Checks — RESOLVED

1. **Resolved:** autopilot's `_autopilot_apply` calls `send_prompt` / `resolve_permission` /
   `answer_question` directly with `SYSTEM_IDENTITY`; only the ws.py handlers carry a real user
   identity. Stamping at the ws.py layer structurally excludes autopilot's own actions. ✔
   (REST/RPC `send_prompt` callers exist but are non-interactive and out of v1 scope.)
2. **Resolved:** the `SessionCard` reads the REST `/api/sessions` poll via `_record_to_dict()`
   (`server/api/sessions.py:96`); `autopilotCooldownUntil` is emitted there, no new channel.
