# Owned retries and partial closeout results

## Symptom

An explicit proposal tap which found no account retained its batch but could no longer be retried after the queue refreshed. Separately, failed issue-close calls could finish and release a batch's ownership as though its closeout had succeeded.

## Anti-pattern

A public retry path restored a durable batch only for its original proposal flag, ignoring the later waiting-account state. A batch effect aggregator interpreted any successful sibling—or simply returning from its loop—as completion without accounting for failed or unobserved intended writes.

## Fix shape

Restore the complete pending batch for both proposed and owned waiting-account states; leave operation identity, unstarted phase and target-set validation to the canonical claim transaction. Test through the real public action provider after an empty refreshed queue, retaining secondary targets and attempt/title history.

Count incomplete linkage or failed intended writes explicitly. A successful sibling cannot make these terminal: return uncertainty and retain ownership for read-only reconciliation. Preserve explicit open-by-policy tracking/unlinked members; they are not failed close attempts. A human gate remains a human handoff even when another member closed successfully. Test the real closeout helper through the real SQL-backed sweep with both all-failed and partially-failed writes.

## Where to look

- `backlog_runner_implement_core`, `AutomationContext.claim_target_state`, and state-driven action builders for transitions which replace `proposed` with another pending state.
- `close_batch_issues` and callers which release ownership or finalize outcomes from one batch result.
- Sibling audit: the generic manual dispatch path already restores persisted batch metadata and shares canonical claim validation. Orphan reconciliation already retains unknown owned waiting-account work; no timer fallback was added.
- Fixed visibility sibling with parent authorization: `BacklogRunnerActionProvider.actionable` requests pending batches through the same `backlog_candidates` read seam and shared `is_pending_dispatch` predicate used by the public action. The normal manual batch picker still receives only unclaimed candidates, so it cannot accidentally select a subset of an existing batch. A waiting-account action is labeled “Retry implementation.”
- Other close callers inspected: CI/CD Doctor records `issue_closed` only after success; Deploy Monitor's normal recovery branch checks success. Its muted-detector cleanup calls close best-effort before discarding source state; that is a separate policy-cleanup lifecycle, reported for follow-up rather than silently changing it in runner ownership work.

## Recovery observer acknowledges before the launch callback

**Symptom:** A healthy native session becomes uncertain or consumes no dispatch budget when recovery observes it first.

**Anti-pattern:** Recovery mutates the launch state independently, while the callback assumes exclusive acknowledgement and accounts only after matching its obsolete snapshot.

**Fix shape:** Idempotent acknowledgement of the exact reservation; durable pre-effect intent; observer and callback settle state/history/accounting under the same ownership lock; reconstruct process budget from persisted admission timestamps. Preserve original snapshot comparison before first settlement and never overwrite later decisions on callback replay.

**Where to look:** Runner launch recovery, PR accounting, Session binding. PR sibling is owned by the PR integration task. Intake/triage local budget counters have no matching ownership observer in this change.
