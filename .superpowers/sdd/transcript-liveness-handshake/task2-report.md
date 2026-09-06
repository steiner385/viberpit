# Task 2 report — Web: rev-watermark liveness probe in `SessionSocket`

**Plan:** `docs/superpowers/plans/2026-08-29-transcript-liveness-handshake.md` § Task 2
**Branch:** `ccswitch/transcript-liveness`
**Files:** modified `web/src/ws/client.ts`; created `web/src/ws/client.liveness.test.ts`

## Problem (H4)

`_healActiveSession` is only reached from the `batch` case via `needsHeal`, i.e. from a seq hole
*inside frames that arrive*. A wedged daemon event reader or a dead server-side forwarding task
produces total silence, and silence contains no gaps — so the transcript freezes and no client-side
mechanism ever notices. The nav-away-and-back workaround was the only recovery.

## Mechanism

The cached session row's `revAt` is kept fresh by the 5s sessions poll and by lifecycle status
patches (`_patchSessionStatus` / `_patchSessionAccount`) — channels wholly independent of the
per-session transcript stream. A `revAt` that advances past the rev the stream last demonstrably
delivered, while the stream has been silent, is positive evidence that the stream (not the session)
is stale. That is the heal trigger; the heal itself is the existing detach+attach
`_healActiveSession`.

## What was added

- Module constants `LIVENESS_CHECK_MS = 5_000`, `LIVENESS_STALE_MS = 15_000` (exported, next to
  `CONNECTION_STABLE_MS`).
- Instance state `_syncedRevAt: string | null`, `_lastSessionFrameAt: number`,
  `_livenessTimer` (next to `_gap`).
- `connect()` starts the interval idempotently (`if (this._livenessTimer === null)`) — `connect()`
  re-runs on every reconnect and one interval per socket lifetime is what is wanted, since the probe
  is itself inert unless there is an active session on a connected socket. `destroy()` clears it
  alongside `_reconnectTimer`.
- Watermark updates: in `_handleInbound`'s `case 'batch'` when `dispatch.length > 0` (frames for the
  active session arrived → stream is live, adopt the cache's current rev), and in the public
  `attach()` right after `_activeSessionId = sessionId` (baseline a fresh view so pre-attach
  staleness cannot fire instantly).
- `_cachedRevAt(sessionId)` reads the row out of `QK.sessions` via `this._queryClient`.
- `_livenessCheck()` next to `_healActiveSession`.

## Design points honoured

1. **Watermark advances BEFORE the heal.** `this._syncedRevAt = revAt` precedes
   `this._healActiveSession()`. If the heal's replay happens to produce no frames, the batch-case
   watermark update never runs — so without this ordering the same unchanged rev would satisfy
   `revIsNewer` on the very next tick and heal again, forever. This is the difference between a
   self-heal and a heal loop, and it is what the 4th test pins (`_syncedRevAt === T2`, second
   `_livenessCheck()` sends nothing).
2. **Silence alone is not staleness.** Both `revIsNewer(revAt, this._syncedRevAt)` and
   `Date.now() - this._lastSessionFrameAt >= LIVENESS_STALE_MS` are required. An idle session is
   silent and correct — test 2 (static rev, 60s silence → no heal) pins that; test 3 (advanced rev,
   1s silence → no heal) pins the converse.
3. **Inert when it cannot be meaningful:** returns immediately with no `_activeSessionId` or a
   `_connectionState !== 'connected'` socket (test 5).
4. **Routes through `_healActiveSession`,** which keeps sole ownership of `HEAL_COOLDOWN_MS`
   (2s) and the `HEAL_LOOP_WINDOW_MS`/`HEAL_LOOP_THRESHOLD` loop escalation. Nothing is duplicated
   or bypassed; the probe adds a second *trigger*, not a second heal path.

## Reuse (no parallel implementations)

- Rev comparison uses `revIsNewer` from `@/session/reconcile` — the same source of truth the poll
  reconciler and (via `shouldApplyRev`) the cache patches use. The import line in `client.ts` was
  extended rather than a new one added; no new rev-parsing helper was written.
- `QK.sessions`, `type Session`, `clientLogger`, `this._queryClient` all pre-existed.

## Verification (all run as `(cd web && …)` subshells)

| Command | Result |
|---|---|
| `npx vitest run src/ws/client.liveness.test.ts` (RED, before implementing) | `Test Files 1 failed (1)` / `Tests 5 failed (5)` — all five `TypeError: s._livenessCheck is not a function` |
| `npx vitest run src/ws/client.liveness.test.ts` (after) | `Test Files 1 passed (1)` / `Tests 5 passed (5)` |
| `npx vitest run src/ws` (neighbours) | `Test Files 18 passed (18)` / `Tests 138 passed (138)` |
| `npm run typecheck` (`tsc -b`) | clean, no output |
| `npm run lint` (`eslint . --ext ts,tsx --report-unused-disable-directives --max-warnings 0`) | clean, no output |

The full `npm run test` was deliberately NOT run — the coordinator runs the full gate (Task 4).

## Adaptations from the plan

None of substance. The plan's test file and implementation snippets applied verbatim; the fake-timer
fallback the plan anticipated (`vi.advanceTimersByTime(3000)` for the `HEAL_COOLDOWN_MS`) was not
needed, because `_lastHealAt` starts at `0` while `Date.now()` under vitest fake timers starts at
the real system epoch, so the cooldown is never armed on a test's first heal. The only additions
beyond the plan text are two WHY-comments (the idempotence rationale on the interval start, and the
"both conditions required" note on `_livenessCheck`).

## Scope

Only the two files in scope were touched. `server/`, `web/src/session/useSessionRuntime.ts` and
`web/src/ws/client.test.ts` were left alone (Tasks 1 and 3, running concurrently / after).

## Note for Task 3

Task 3 also edits `web/src/ws/client.ts` (extracting `ws.onmessage` into `_onRawMessage`). Its
target lines are untouched by this change — the `ws.onmessage` handler is still the original inline
`try/JSON.parse/catch` block; the liveness interval start sits a few lines above it, right after
`this.ws = ws`.
