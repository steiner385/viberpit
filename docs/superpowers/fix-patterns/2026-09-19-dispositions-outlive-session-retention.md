# Work dispositions must outlive session retention

## Symptom

PM map-diff sessions verified a suggestion was already covered, archived, and
immediately re-spawned. The archive cap purged automation sessions first, deleting
the completed row milliseconds after it was written (#2008, follow-up to #1962).

## Anti-pattern

Using an execution session as the only durable record that its source work has
been resolved. Unit tests retained the archived row, so they never exercised the
cleanup transition that erased the decision.

## Fix shape

Persist the disposition on the source proposals before completion/cleanup, with
the evaluated input identity and evidence. Session lifetime becomes irrelevant.
Compare current inputs with that identity so new or changed work remains eligible.
Verify the whole resolve/archive/delete/restart/dispatch lifecycle, including
negative controls for uncovered work and explicit human holds.

## Where to look

- PM triage cluster construction, repository observation, manual archive, and
  archive-cap permanent deletion.
- Sibling audit: `AutomationContext._existing_session` is intentionally an
  in-flight exclusion, not a completed-work disposition. Keep that distinction.
- `AutomationTargetState` persists retry/outcome state separately from sessions;
  `DispatchOutcome` similarly stores input fingerprints and final results. Neither
  depends on retaining the execution session row for its decision history.
- Recent-session reads in fleet summaries and archive audit describe retained
  sessions; they do not suppress source work and need no change for this pattern.
