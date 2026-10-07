# Autopilot reliability investigation and repair

Tracking: #2738. The operator reported that neither Codex nor Claude sessions
were reliably advancing. Autopilot was enabled in auto mode and still taking
some actions; the shared assistance path had several independent defects.

## Evidence

- Three human-deference ticks at 10, 30, and 50 seconds raised the repetition
  counter to three without taking any action. At 130 seconds the session was
  escalated as stuck instead of receiving its first continuation.
- At 11:11:43 UTC on October 7, Tasks UI session `44e2ed76` was escalated because
  its facts omitted KinDash PR #18284. GitHub showed that PR merged at 11:02:49.
  The session had created it in an additional worktree whose branch was absent
  from the original worktree's HEAD reflog and persisted branch list.
- PR refresh happened only after a nudge decision. An escalation bypassed it,
  and any older armed/merged PR suppressed contradiction checks for newer work.
- The periodic assistance task was primary-only while eligibility was scoped
  to the session's owning host. Workers had status-change triggers but no
  guaranteed retry after a deferred tick.
- The daily action counter lived in each process despite its global label.
  The edge also displayed the primary's assistance observation without workers.

## Repair boundaries

Observation is not an assistance attempt. Skips preserve the attempt allowance;
new human input starts a new episode once. Legacy repetition state is reconciled
once, retaining budgets and safety constraints. Actual repeated failures and
futile composition remain bounded.

Contradictory PR facts are verified before model judgment, after hard guardrails.
Repository identity is preserved. Explicit artifacts and verified creation
activity establish linkage; arbitrary prose mentions and ambiguous compound
shell success do not. A failed evidence read is unavailable, not verified absence.
Completion still requires the relevant goal to be met.

Periodic assistance belongs on every session-owning host. External actions must
recheck authority after asynchronous work. The fleet-wide action allowance is a
durable, atomic reservation using existing automation state, not an event count
or a second process-local budget. Observation aggregation preserves producer
freshness, missing hosts, partial failures, and human waits.

Explicit hand-backs, manual pauses, product policy, destructive-command denials,
permission checks, and manual-unarchive protection remain in force.

## Verification record

Implementation regressions cover the reproduced defer sequence, real failure
bounds, legacy state, PR identity/linkage, refresh failure, worker retries,
authority changes, and unavailable/degraded fleet observations. Budget concurrency
was exercised against both SQLite and a dedicated PostgreSQL test database.

A live transport smoke used the changed control loop and public session API with
real Claude and Codex turns on Rocky. Three accelerated deference checks sent
nothing; after simulated expiry, exactly one continuation completed on each
native engine. Requested primary placement was redirected to Rocky by capacity
policy, so this is not evidence of a primary-host live smoke. Test sessions were
left complete with assistance paused and auto-archive disabled; no archive or
retention setting was changed. Full gate and rollout results are recorded in the
fixing PR.

## Sibling audit

The retry-counter pattern was confined to autopilot. Its missing-composer branch
also needed bounded futile-attempt accounting and was corrected. The global
budget audit found process-local spawn counters in PM triage and SDLC intake;
those loops remain primary-only and are outside this multi-host assistance change.
The backlog runner already recovers dispatch accounting from durable state.
