# Model switch: stop-and-restart on busy sessions, persist-and-apply on inactive ones

**Date:** 2026-07-07
**Status:** Approved for implementation

## Problem

The session model picker (`SessionMeta` → `/model` → `set_model_for_session()`) works only for
idle live sessions. Three gaps make it feel broken elsewhere:

1. **Busy session:** `driver.set_model()` affects only future turns. Switching mid-turn leaves
   the in-flight activity running on the old model with no feedback.
2. **Inactive (detached) session:** `run_command()` returns "This session isn't live — open it
   and try again" before the `/model` branch is reached, so the picker is a silent no-op even
   though `set_model_for_session()` would persist fine with no driver.
3. **Resume:** `ensure_driver()` (detached → live) never re-applies `rec.model` after
   `driver.start()` — unlike `_reassign()`, which does — so a persisted model is dropped on
   resume.

## Desired behavior (user requirement)

- Toggling the model while activities are in progress: **confirm with the user**, then
  immediately **stop the current activity**, **change the model**, and **resume** the activity.
- Toggling the model on an inactive session: **persist it in the DB** so the session uses it
  when resumed/started.

## Design

### Backend (`server/sessions/`)

- **`driver.py`** — add a public `turn_active` property returning `self._turn_active` (the
  existing ground truth used by the stall watchdog and `pending_foreground_prompt()`).
- **`orchestrator.set_model_for_session(session_id, model) -> bool`** — returns whether the
  in-flight turn was restarted. When a live driver exists **and** `driver.turn_active`:
  `await driver.interrupt()` → `await driver.set_model(model)` → re-send
  `self._last_prompt[session_id]` (if any). The driver's FIFO prompt queue runs the resend as a
  fresh turn after the interrupt unwinds — the same stop → switch → replay dance `_reassign()`
  performs across drivers during account rotation, here on the same driver. Idle live sessions
  keep today's behavior (just `set_model`); record update + `_persist_model` happen in every
  case, including driverless.
- **`orchestrator.run_command()`** — hoist the `/model` branch above the `d is None` guard.
  Notice text per case: restarted → "Model → X — stopped the current activity and restarted it
  on the new model"; no driver → "Model → X — saved; applies when the session resumes";
  otherwise → "Model → X" (unchanged).
- **`orchestrator.ensure_driver()`** — after the driver is started and registered, apply
  `if rec.model is not None: await driver.set_model(rec.model)`, mirroring `_reassign()`.
  Placed after the fresh-start fallback so both resume paths get it.

"Resume the activity" means re-sending the interrupted turn's prompt: mid-generation resume is
not possible, and turn-restart-from-prompt is the established rotation semantics
(`turnRestarted` in the `rotated` event).

### Frontend (`web/src/`)

- **`components/session/ModelSwitchDialog.tsx`** — small confirm dialog patterned on
  `PermanentDeleteDialog` (caller opens it; action runs only on confirm). Copy: the session is
  working; switching stops the current activity and restarts it on the new model. Buttons:
  "Stop & switch" / "Cancel".
- **`SessionBody.tsx`** — `onSelectModel`: when `statusTier(rt.state.status.state) === 'busy'`,
  stash the pending model id and open the dialog; confirm sends `rt.onCommand('model', id)`,
  cancel discards (the `<select>` is controlled by the record model, so it snaps back on its
  own). Non-busy tiers send immediately, as today.

Typed `/model` slash commands stay immediate (an explicit command is its own confirmation);
only the picker gates on the dialog.

## Error handling

- Interrupt that fails to land: existing stall-watchdog machinery owns that case (`drain_alive`
  escalation); the model is persisted regardless, so the next turn/driver uses it.
- No `_last_prompt` (e.g. daemon restarted mid-turn): interrupt + set_model still run; nothing
  is resent; notice reports a plain switch. Restart flag stays false.

## Testing

- **Server (pytest):** busy live session → interrupt called, model set + persisted, last prompt
  re-sent, returns True; idle live → no interrupt/resend; detached `run_command('/model')` →
  persists + "applies when the session resumes" notice; `ensure_driver()` re-applies persisted
  model after resume.
- **Web (vitest):** busy tier → dialog opens, confirm sends the command, cancel sends nothing;
  non-busy → command sent immediately, no dialog.

## Out of scope

- No new RPC surface: `/model` already flows through the RPC'd `run_command`.
- No changes to `ModelSwitchDivider` (per-turn model tracking already renders the divider once
  the restarted turn runs on the new model).
- Queued-prompt reordering on restart (the resend joins the existing FIFO queue) — rare edge,
  same as rotation today.
