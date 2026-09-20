# A refusal is a state, not an event

## Symptom

One stuck session produces hundreds of identical warnings and an auto-filed error
issue whose occurrence count keeps climbing while nothing changes. Session
`331f7d98` logged `session.lease_denied {"holder": "dobby", "reason": "relaunch
(weekly limit reached)"}` 199 times over eight days (#1778) — one line per retry, all
of them the same fact: rocky owned the row, dobby held the lease (#1824).

## Anti-pattern

Two halves, and the storm needs both.

**Recording a handled refusal as a retryable failure.** `_reassign_locked` classified
`SessionLeaseHeld` with the default arm: `log.exception("reassign.error")` and a park
under `reassign_failed:<trigger>` — precisely the reason the stuck-session sweep
exists to retry. Each retry was refused identically and re-parked. This is the #1767
shape (`automation_ownership_stale`) recurring in the branch right next to the fix
for it, because that fix enumerated one exception class rather than the property
("the refusing condition will still hold on the next attempt").

**Logging a steady state at WARNING per attempt.** A refusal that repeats is not news
after the first one, but the log line cannot tell — so the volume tracks the retry
cadence rather than the number of distinct problems, and error triage files an issue
sized by how often a loop spins.

## Fix shape

Classify by whether the condition can change, and log by whether the fact has.

- **Settled forever** → `_TERMINAL_PAUSE_REASONS` (`automation_ownership_stale`,
  `engine_rotation_unsupported`): never retry.
- **Settled for now** → its own pause reason plus a predicate the retry loops consult.
  `_LEASE_HELD_PAUSE_REASON` ("lease_held") with `_lease_held_elsewhere()`, which asks
  exactly what `session_lease.acquire` refuses on (a different holder, `age_s <
  SESSION_LEASE_TTL_S`) without taking the lease. Both automatic loops —
  `_sweep_needs_attention` and `_restart_driverless` — back off while it is true and
  recover normally once the lease lapses. Making it terminal instead would strand a
  session whose owning daemon died, which is the case the lease TTL exists for.
- **Handled, not faulted** → WARNING without a traceback. `log.exception` on a fence
  working as designed files a second issue for the same event.
- **Steady state** → one log line per distinct fact. `_lease_denial_is_news(sid,
  holder, layer)` keeps the first refusal and every change of holder or layer at
  WARNING with its structured `session.lease_denied` event; identical repeats drop to
  DEBUG until a claim succeeds or the lease is released. Keyed on the fact, not on a
  timer, so a holder that changes is never hidden by an unexpired window.

The gate is on the pause reason first, so the common path reads no lease row; the
predicate fails open (False) on a DB error, leaving the pre-fix behaviour — attempt,
be refused — rather than parking every session on a blip.

## Where to look

- Fixed: `_reassign_locked`'s failure classification, `_restart_driverless`,
  `_sweep_needs_attention`, `_claim_session` (both refusal sites) in
  `server/sessions/orchestrator.py`.
- Already correct: `ensure_driver`'s resume claim returns None and parks nothing, so
  it never fed a loop; it shares the de-duplicated logging.
- Same shape, still open: `_reconcile_foreign_lease` logs CRITICAL once per tick while
  a reconcile is deferred (#1832) — a steady state on a cadence, the second half of
  this pattern without the first.
- Companion: `2026-09-15-fleet-mirrors-are-not-local-actors.md` covers who may act on
  a row at all. This note covers what to do once the answer is "not you".
