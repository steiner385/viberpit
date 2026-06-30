# Dropped-turn recovery — escalate, soften, surface

**Date:** 2026-06-30
**Status:** Design approved, pending spec review

## Problem

A turn that ends with **no assistant output at all** is tagged by the Claude CLI as
`[ede_diagnostic] … stop_reason=null`. ccswitch classifies this as `turn_dropped`
(`server/sessions/failure.py:92`) and auto-resends the last prompt on the same account,
capped at 3 resends / 120s (`server/sessions/recovery.py:36`, `orchestrator.py:2334`).

Observed failure (session at **ctx 92%**, see report screenshot 2026-06-30): the resend
loops because each attempt re-sends the same prompt into the same near-full context, which
drops again. The user sees:

1. A red `Error (execution_error)` banner **and** an inline red `ErrorCard` on **every**
   attempt (the auto-heal looks like a hard failure).
2. The loop runs 3× then, at the cap, `_recover_session` does a bare `return` — the session
   silently gives up with **no surfaced state or next step**.

The underlying `stop_reason=null` originates in the Claude binary/API returning an empty
turn; per constitution Principle I we never patch the binary, so we cannot stop the API
emitting it. What we *can* fix: the futile resend-into-full-context, the alarming UI, and
the silent give-up.

## Goals

- Stop resending blindly into a context that just dropped a turn — escalate to a compaction.
- Make in-flight auto-recovery read as *recovering*, not *failed*.
- Make the give-up (cap reached) an explicit, actionable surface, not silence.

## Non-goals

- Patching or working around the binary's `stop_reason=null` emission (Principle I).
- Reading/forcing a live ctx% at recovery time (the escalation ladder infers context
  pressure from behavior instead — see Design rationale).
- Changing recovery for the *other* ede variant (`stop_reason=tool_use`, orphaned tool_use),
  which `transcript_repair` already heals on resume.

## Design

### Behavior ladder

Keyed on the existing per-session resend counter (`recovery.py`, 3 resends / 120s window).
The count of resends already recorded in the window selects the action:

| Resends so far in window | Action on this drop |
|--------------------------|---------------------|
| 0 (first drop)           | Record resend; **plain** `_replay_same_account` (cheap — handles a transient drop) |
| 1 or 2                   | Record resend; send `/compact` then `_replay_same_account` (context-pressure escalation) |
| cap reached (3)          | **Do not resend.** `_pause_session(reason="turn_dropped_cap")` → `needs_attention` |

Rationale for behavior-as-signal: a plain resend that drops *again* is strong evidence the
context did not clear, without needing a reliable ctx% reading at recovery time. The first
cheap resend absorbs genuinely transient drops; compaction is spent only once a plain resend
has proven insufficient.

`/compact` is sent via `driver.send_prompt("/compact")`; `commands/dispatcher.py:29` already
streams `/compact` as a normal turn (the SDK/binary performs the compaction; we observe it via
the existing `PreCompact` hook → `Condensing` status). The compaction turn completes, then the
resend of the user's prompt follows.

### Component 1 — Driver splits the dual-duty ErrorEvent

`server/sessions/driver.py:548-553`. Today the `turn_dropped` `ErrorEvent` does double duty:
it drives recovery classification (`_maybe_signal_recovery`, which reads `raw_message`) **and**
is forwarded verbatim to the UI (`_out.put(e)` → red banner + `ErrorCard`).

Change: when the forwarded event is an `ErrorEvent` that `classify()`s as `turn_dropped`, the
driver emits a **new soft `Recovering` event** to the UI in place of the red `ErrorEvent`, while
still calling `_maybe_signal_recovery` unchanged. The translator stays a pure message→event
mapper (no knowledge of resend caps); the recovery context lives in the driver/orchestrator,
which is where the split belongs.

The `Recovering` event carries: `turn_id`, and a short human reason
(`"Recovering the dropped turn…"`). It is informational only — it never raises `state.error`.

### Component 2 — Escalation + give-up in the orchestrator

`server/sessions/orchestrator.py:2334` `_recover_session`, `kind == "turn_dropped"` branch:

```
count = self._recovery.resend_count(record.id, now=now)   # new read-only helper
if self._recovery.resend_cap_reached(record.id, now=now):
    await self._pause_session(record, reason="turn_dropped_cap")
    return
self._recovery.record_resend(record.id, now=now)
if count >= 1:
    await self._replay_same_account(record, 0.0, precompact=True)   # /compact then resend
else:
    await self._replay_same_account(record, 0.0)                    # plain resend
```

`_replay_same_account` gains an optional `precompact: bool = False`; when set, it sends
`/compact` (via the driver) before re-sending the stored last prompt. (Exact send ordering to
be settled in the plan — the prompt resend must wait for the `/compact` turn to finish so they
don't interleave.)

`record_resend` / `resend_cap_reached` already exist; add a read-only `resend_count(session_id,
now)` that returns the in-window count (the cap check already computes this; factor it out).

### Component 3 — Frontend renders the soft state

- `server/sessions/events.py`: add the `Recovering` event dataclass + its wire serialization,
  mirroring the existing lightweight events.
- `web/src/session/reducer.ts:628`: handle the new event as a `kind: 'recovering'` inline block
  appended to the turn, and a soft (non-error) status line. It must **not** set `state.error`
  (contrast the `error` case at line 628, which raises the banner).
- New `web/src/components/session/RecoveringCard.tsx` (sibling of `ErrorCard.tsx`): a muted,
  non-red "Recovering the dropped turn…" row.
- The cap-hit pause flows through the existing `needs_attention` path
  (`statusTier.ts:35` → red "Turn failed"); sharpen the surfaced message to indicate context may
  be full. No new give-up component required.

## Data flow

```
binary → ResultMessage(stop_reason=null)
  → translator.feed → ErrorEvent(code=execution_error, raw_message="[ede_diagnostic]…stop_reason=null")
    → driver._drain:548
        ├─ classify → turn_dropped → _maybe_signal_recovery → on_recoverable_failure → orchestrator
        │     └─ _recover_session: ladder (plain resend | /compact+resend | pause)
        └─ emit Recovering event (NOT the red ErrorEvent) → UI shows "Recovering…"
  … if cap reached → _pause_session → needs_attention → UI red "Turn failed — context may be full"
```

## Error handling

- `/compact` itself failing/dropping: counts as the next drop in the window and falls through the
  same ladder; the cap still bounds total attempts, so a persistently-broken session pauses rather
  than looping.
- A drop for a non-context reason (transient API hiccup): absorbed by the first plain resend; only
  repeats escalate to compaction. No regression vs today's behavior for the common transient case.
- Classification path is unchanged (still reads `raw_message`), so existing recovery tests for the
  `turn_dropped` kind remain valid.

## Testing

- `failure.py` classification of `stop_reason=null` → `turn_dropped`: unchanged, keep existing tests.
- `recovery.py`: new `resend_count` helper — count within/outside window; cap boundary.
- `orchestrator` `_recover_session` ladder: first drop → plain resend; second → precompact resend;
  cap → `_pause_session` (assert on a real manager, not just a fake — per the recovery facade fan-out
  note). Verify `/compact` is sent before the prompt resend on the escalated path.
- Driver: a `turn_dropped` `ErrorEvent` emits a `Recovering` event to `_out` and still calls
  `_maybe_signal_recovery`; a non-dropped error still emits the red `ErrorEvent`.
- Reducer: `recovering` event appends a `recovering` block and does **not** set `state.error`;
  the after-cap `needs_attention` still raises the red surface.
- Full server suite + full vitest + `tsc -b` (a new event touches both wire serializers and the
  reducer; signature/shape changes are exactly what per-file runs miss).

## Affected files

- `server/sessions/driver.py` — split dual-duty event; emit `Recovering`.
- `server/sessions/orchestrator.py` — escalation ladder + pause-after-cap; `_replay_same_account`
  `precompact` arg.
- `server/sessions/recovery.py` — `resend_count` helper.
- `server/sessions/events.py` — `Recovering` event + serialization.
- `web/src/session/reducer.ts` — `recovering` block kind; do not raise `state.error`.
- `web/src/components/session/RecoveringCard.tsx` — new soft card.
- `web/src/components/session/SessionBody.tsx` / status surface — wire the new block + sharpen the
  cap message.
