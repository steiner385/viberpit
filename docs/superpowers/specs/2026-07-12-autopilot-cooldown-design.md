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
| Duration | **120s**, exposed as a configurable autopilot config field. |
| Visibility | **UI badge** on the session ("Autopilot paused — you're steering (Nm left)"). |

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

New autopilot config field in the autopilot automation schema (`server/automations/autopilot.py`):

```python
ConfigField(
    key="user_interaction_cooldown_seconds",
    label="User interaction cooldown",
    type="int",
    default=120,
    help="Seconds after the user's last interaction before autopilot may act on that session.",
)
```

### The gate

In `pre_decide()` (`server/automations/autopilot_policy.py`), as the **first** check — ahead of
deny-list, hourly budget, and loop detection — so a steering human is never blocked on a lower gate.
`last_user_interaction_at` flows into the `DecisionContext` built by
`_autopilot_get_context()` (`server/sessions/orchestrator.py`), keeping the policy function pure and
unit-testable:

```python
cooldown = int(config.get("user_interaction_cooldown_seconds", 120))
last = ctx.last_user_interaction_at
if last is not None and (now - last).total_seconds() < cooldown:
    remaining = cooldown - int((now - last).total_seconds())
    return Decision(action="skip", reason=f"user steering (cooldown {remaining}s remaining)")
```

The skip is logged like any other autopilot skip (no new logging surface required).

### UI badge

The frontend already syncs session status/records to `SessionCard`. Expose
`autopilot_cooldown_until` on that payload; the card renders a **non-interactive** badge such as
**"Autopilot paused — you're steering (1m left)"**, counting down locally and disappearing once the
timestamp passes.

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

- `server/sessions/types.py` — add `last_user_interaction_at` to `SessionRecord`.
- `server/bridge/ws.py` — stamp on `prompt` / `permissionResponse` / `answerQuestion`; handle new
  `activity` message.
- `server/automations/autopilot.py` — add `user_interaction_cooldown_seconds` config field.
- `server/automations/autopilot_policy.py` — the first-gate cooldown check in `pre_decide()`.
- `server/sessions/orchestrator.py` — attach `last_user_interaction_at` to `DecisionContext` in
  `_autopilot_get_context()`; expose `autopilot_cooldown_until` in the session status DTO.
- Session status DTO / serializer (wherever the status payload is assembled).
- `web/src/…` — `SessionCard` badge; prompt-input `activity` emitter + throttle; WS client message
  type + any status-type additions.
- Tests as listed above.

## Open Implementation Checks

1. Confirm autopilot `apply()` routes injected actions through the shared orchestrator methods
   (validates the stamp-at-WS-layer decision) and that no user-facing send path bypasses ws.py.
2. Confirm the exact session status DTO/serializer where `autopilot_cooldown_until` should be added
   so the existing frontend sync carries it without a new channel.
