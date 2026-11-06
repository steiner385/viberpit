# Session snooze — design

Date: 2026-11-06 · Status: approved by owner ("just start"; design calls below are the builder's)

## Intent

Some sessions are not finished and should not be archived, but will sit idle until a
distant event happens: a PR lands, a deploy soaks, a vendor replies, a date arrives.
Today they clutter the live list, count toward "needs you", and get nudged by
autopilot. Snooze puts such a session out of sight until a time (or until someone
wakes it), then brings it back. A session can snooze **itself**, so an unattended
agent waiting on an external event can sleep instead of handing back or busy-polling.

## Goals

- User snoozes a session from its card: 1 hour, tomorrow 9am, next Monday 9am, or
  "until I wake it". A snoozed session leaves the main list for a collapsed
  **Snoozed** section, and can be woken from there.
- Session snoozes itself with a `snooze(until, reason)` session tool. At `until` it
  receives a `<snooze-wake>` prompt carrying its own reason, so it can re-check the
  event and continue, snooze again, or hand back.
- Any human prompt to a snoozed session wakes it.
- While snoozed, automation leaves the session alone: autopilot does not govern it,
  completion auto-archive does not archive it, and a self-snooze ends its turn as
  "Waiting…" (`waiting_external`), not "Your turn".

## Non-goals (v1)

- Event-triggered wakes (e.g. "wake when PR #N merges"). A session gets this by
  snoozing for a sensible interval and re-checking on wake.
- Snoozing queued / pending-approval (pre-spawn) sessions.
- Assistant-drawer tools for snooze.

## Anti-goals

- A session that sleeps forever unnoticed: a self-snooze must name a time (≤ 30
  days); only a human may snooze indefinitely.
- A new session status: snooze is an overlay on the record, so every status map,
  tier and badge keeps working unchanged.

## Design

**State.** One nullable JSON column `sessions.snooze` (reconciled automatically on
both dialects) and `SessionRecord.snooze`:
`{"at": iso, "until": iso | null, "reason": str | null, "by": "user" | "session"}`.
`null` = not snoozed. Emitted as `snooze` by both serializers (`SessionRecord.to_dict`
daemon→edge, `_record_to_dict` edge→browser).

**Orchestrator** (`server/sessions/orchestrator.py`, runs on the owning daemon):
- `snooze_session(session_id, *, until, reason, by)` — validates, writes record + row,
  audits `session.snooze`.
- `wake_session(session_id, *, cause)` — clears, audits `session.wake`; if the snooze
  was the session's own (`by == "session"`) it sends the `<snooze-wake>` prompt.
- `_snooze_wake_loop` (60 s) wakes every due record it owns. A session-snooze whose
  prompt cannot be delivered (deploy drain) stays snoozed and is retried next tick.
  A user snooze that ends sends a push notification.
- `send_prompt` with a human `command_origin` wakes a snoozed session (cause `prompt`).
- Exclusions: `_session_is_parked` reports a snoozed session as parked; the
  autopilot candidate sweep withholds it (`"snoozed"`); `_maybe_autoarchive_completed`
  skips it.
- Routed like `set_auto_archive_opt_out`: RPC allowlist, `RemoteOrchestrator`,
  facade, fleet `_PER_SESSION` + `_MOVE_BLOCKED`.

**API.** `POST /api/sessions/{id}/snooze {until: iso | null, reason?: str}` and
`DELETE /api/sessions/{id}/snooze` → the session dict. 404 unknown, 409 pre-spawn or
archived, 422 bad time (past, or > 365 days out).

**Session tool.** `snooze(until, reason)`, SIGNAL class (no permission card), mounted
with the other signalling tools on both engines. Not a turn signal (the ToolCall card
is the transcript record). `until`: ISO-8601, 5 minutes to 30 days out. After calling
it the session ends its turn without `mark_complete` / `hand_back`.

**Web.** `snooze` on the `Session` type; `useSnoozeSession` / `useWakeSession`
hooks; snoozed sessions are held out of the grouped list (like queued rows) into a
collapsed-by-default `SnoozedSection` showing name, wake time, reason, who snoozed,
and a Wake button; the card overflow menu gets Snooze presets.

## Builder's calls

- JSON column over four scalar columns: one field through every codec.
- The 30-day self-snooze cap mirrors `hand_back(recheck_at)`.
- A user wake of a self-snoozed session still sends the wake prompt ("check now").
