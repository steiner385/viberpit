# Moving an automation loop across hosts changes its safety boundary

- **Symptom:** a worker never retries a deferred session; enabling its periodic loop then multiplies a supposedly global action allowance or lets stale decisions overwrite the new owner’s ledger.
- **Anti-pattern:** moving a task out of the primary-only block while retaining process-local budgets, monotonic persisted timestamps, cached ownership, or unrelated bundled mutations.
- **Fix shape:** retain one local trigger lock, use a shared atomic budget reservation and a non-charging check before model work, persist epoch timestamps, and verify authoritative host/lease plus the human revision before applying decisions or storing their ledger. After a successful action changes status, relax only the observation status check; retain ownership, lease generation, and human-input checks. A denied authority check must not write its stale ledger back. Keep separately governed completion cleanup on its existing primary-only schedule until its mutation path has appropriate fencing.
- **Where to look:** `AutopilotLoop`, `AutomationContext` budget methods, `_autopilot_may_act`, `_autopilot_tick_loop`, `_PRIMARY_ONLY_LOOP_ATTRS`, and the real-authority fixtures in `test_autopilot_authority.py`.

The sibling audit found process-local spawn histories in PM triage and SDLC intake; both remain primary-only, so this change does not introduce multiple producers there. Backlog dispatch already recovers its rolling history from durable state. These are not reasons to add a second budget or ownership implementation.

Verified by worker periodic/concurrent-trigger regressions, stale-owner ledger and action tests, and SQLite/PostgreSQL final-budget-slot races. See #2738 and the investigation in `docs/audits/2026-10-07-autopilot-reliability.md`.
