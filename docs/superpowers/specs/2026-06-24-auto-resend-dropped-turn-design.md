# Auto-resend a dropped turn (`[ede_diagnostic] … last_content_type=n/a`)

**Date:** 2026-06-24
**Status:** Design — approved, pending spec review

## Problem

A live session turn occasionally dies with:

```
ErrorEvent(code="execution_error",
           message="[ede_diagnostic] result_type=user last_content_type=n/a stop_reason=null")
```

The turn surfaces a red error banner and dead-ends. The user's proven workaround is to
re-send the **exact same last message** (e.g. "keep going"), which succeeds immediately.
We want ccswitch to do that resend automatically.

### Why this is safe to auto-resend — and the exact discriminator

The discriminating signal is **`stop_reason=null`**. The turn ended without the model
committing to any continuation (no `tool_use`, no `end_turn`) — it dropped. Re-sending the
same prompt is idempotent because nothing downstream was committed. The user's screenshot
confirms a manual resend of the identical message succeeds immediately.

`last_content_type=n/a` is NOT the discriminator: the existing translator test
(`test_ede_orphaned_tool_use_gets_a_legible_message`) shows the **unsafe** orphaned-tool_use
case *also* reports `last_content_type=n/a`. The only field that separates safe from unsafe is
`stop_reason`. The classifier MUST therefore gate on the positive, exact token
`stop_reason=null` — which can never overlap `stop_reason=tool_use`.

### What this is NOT

This is a **distinct** `ede_diagnostic` variant from the orphaned-tool_use case
(`stop_reason=tool_use`), which is already healed by `transcript_repair` on resume
(`driver.start`). In that variant a tool *did* run, so a naive resend would be unsafe. Gating
on `stop_reason=null` (not merely "not `tool_use`") also avoids auto-resending any unknown
future `ede` variant with some other `stop_reason` — tighter is safer.

## Approach

Reuse the existing transient-failure recovery pipeline rather than building a parallel retry
path (constitution VI — single source of truth). Today that pipeline already auto-resends the
last prompt for `rate_limit` (`_replay_same_account`) and `process_crash` (`_reassign`):

```
driver._maybe_signal_recovery(code, msg)
  → failure.classify() → TurnFailure(kind)        # kind ∈ _RECOVERABLE ?
  → orchestrator._handle_recoverable_failure
  → _recover_session() dispatches per kind
  → _replay_same_account() → driver.send_prompt(last)   # == "resend last user message"
RecoveryState supplies per-session capped counters (crash_cap=3/120s, …) to bound loops.
```

A "dropped turn" becomes a new recoverable `kind` routed to a **capped, immediate,
same-account resend**.

### Rejected alternatives

- **B — resend inside `driver._drain`.** The driver intentionally delegates retry *policy*
  to the orchestrator via `_on_recoverable_failure`; adding caps + a resend loop in the driver
  splits the single source of truth and is harder to bound across reconnects.
- **C — auto-resend from the web frontend.** Fragile (dies if the tab is disconnected or
  backgrounded — e.g. mobile) and places recovery policy in the wrong layer.

## Design

### 1. `server/sessions/failure.py` — classify the dropped turn

- Add kind `"turn_dropped"` and include it in `_RECOVERABLE`.
- In `classify()`, return `TurnFailure(kind="turn_dropped")` when **all** hold:
  - `code == "execution_error"`, and
  - message contains `[ede_diagnostic]`, and
  - message contains `stop_reason=null`.

  (The `stop_reason=null` token alone excludes the `stop_reason=tool_use` variant; no
  `last_content_type` check is needed and none should be added — both variants share `n/a`.)
- Order: place this check so it does not shadow / is not shadowed by the existing
  429/401/overload/crash inference. `execution_error` + the `ede` marker is unambiguous, so it
  can be checked early alongside the explicit-code branches.

### 2. `server/sessions/recovery.py` — bound the resend loop

- Add `RESEND_CAP: int = 3` and `RESEND_WINDOW: float = 120.0` (mirrors `CRASH_CAP` /
  `CRASH_WINDOW`).
- Add `record_resend(session_id, *, now)` and `resend_cap_reached(session_id, *, now)`
  following the exact shape of `record_crash` / `crash_cap_reached` (windowed list, prune on
  read).

### 3. `server/sessions/orchestrator.py` — recover branch

In `_recover_session`, add before/after the existing kind branches:

```python
if kind == "turn_dropped":
    if self._recovery.resend_cap_reached(record.id, now=now):
        return  # give up quietly — error already surfaced; not an account fault
    self._recovery.record_resend(record.id, now=now)
    await self._replay_same_account(record, retry_after=0.0)
    return
```

- No pause, no quarantine, no rotation — a dropped turn is not an account problem, so on cap
  exhaustion we simply stop and leave the (softened) error visible.
- `_replay_same_account(record, 0.0)` resends `self._last_prompt[record.id]` immediately on the
  same account, reusing existing code unchanged.

### 4. `server/sessions/translator.py` — soften the banner

- Extend `_humanize` to also rewrite the `stop_reason=null` variant (today only
  `stop_reason=tool_use` is humanized). Use a message that reflects auto-recovery, e.g.:
  > "The turn ended before the model responded. Resending your last message automatically…"
- Keep the existing `_EDE_HUMAN` (tool_use) wording for that variant.

### 5. Recovery indicator (UX)

- The resend goes through `driver.send_prompt`, which echoes the user message and starts a
  fresh turn — honest and matching the manual workaround.
- Show a small "auto-resent" indicator so the user understands the new turn was automatic, not
  their own input. NOTE: the existing `_retrying` flag is *within-turn* (set on `ApiRetry`) and
  the resend is a **new** turn, so it does not transfer for free. The exact mechanism (e.g. a
  short-lived recovery flag/event carried from `_recover_session` into the next
  `send_prompt`, or softened-banner text alone) is deferred to the implementation plan; the
  softened banner from §4 is the minimum and is sufficient on its own.

## Loop-safety guards (summary)

1. **Classifier** — precise `stop_reason=null` gate, structurally excludes `tool_use`.
2. **Bound** — `resend_cap_reached` (3 / 120s); on exhaustion, stop and surface.
3. **Idempotency** — `stop_reason=null` ⇒ no continuation committed ⇒ resend duplicates nothing.

## Testing

- `failure.py` (pure): unit tests in `test_failure_classify.py` — the `stop_reason=null`
  variant → `turn_dropped` & recoverable; the `stop_reason=tool_use` variant → NOT
  `turn_dropped` (stays `other`); unrelated `execution_error` → `other`.
- `recovery.py`: `record_resend` / `resend_cap_reached` window + cap behavior.
- `orchestrator`: a `turn_dropped` failure triggers exactly one `send_prompt(last)` per event;
  the 4th within the window does not resend (cap); no quarantine/rotation occurs.
- `translator.py`: the n/a variant is humanized; the tool_use variant keeps `_EDE_HUMAN`;
  code stays `execution_error`.

## Out of scope

- Broadening auto-resend to other `execution_error` shapes or to `stop_reason=tool_use`
  (already handled by transcript repair).
- Any change to the queue, rotation, or quarantine policy.
