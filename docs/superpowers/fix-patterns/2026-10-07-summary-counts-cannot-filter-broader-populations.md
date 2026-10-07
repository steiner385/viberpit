# Summary counts cannot filter a broader detail population

## Symptom

A capability containing only an open, owner-held PR disappeared from session
roadmap triage. Its detail endpoint returned the PR, but the capability's issue
attention count was zero.

## Anti-pattern

Skipping a detail read based on a zero summary count when the summary counts a
narrower population than the detail query. Here the map counts issues, while
capability leaves include both issues and PRs.

## Fix shape

Read the canonical leaf population and filter its actual attention tiers. Do not
use issue counts as an emptiness test for issue-and-PR results. Keep a regression
with a held PR and no issues, rather than a mixed fixture whose issue count hides
the mismatch.

## Where to look

`server/pm/session_triage.py` now reads every capability's leaves.
`server/api/pm_tree.py` owns the differing map and leaf populations.
The bounded sibling audit found no other server consumer using this shortcut.
`web/src/features/roadmap/detail/needsYou.ts` derives its issue groups from map
counts; whether UI attention should also include PRs is a separate scope decision.
`LeafList.tsx` reads leaves directly and does not use the shortcut.
