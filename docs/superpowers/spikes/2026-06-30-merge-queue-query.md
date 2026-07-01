# Spike: repo-level `mergeQueue.entries` shape (M-Track-10, Task 1)

**Date:** 2026-06-30 · verified live against `cairnea/KinDash` (active merge queue).

## Confirmed query

```graphql
query {
  r0: repository(owner: "cairnea", name: "KinDash") {
    mergeQueue {
      entries(first: 50) {
        nodes {
          position
          state
          enqueuedAt
          estimatedTimeToMerge
          headCommit {
            oid
            statusCheckRollup {
              state
              contexts(first: 100) {
                totalCount
                nodes {
                  __typename
                  ... on CheckRun { status conclusion }
                  ... on StatusContext { state }
                }
              }
            }
          }
          pullRequest { number }
        }
      }
    }
  }
}
```

Captured response: `server/tests/fixtures/merge_queue_kindash.json` (3 live entries).

## Field findings

| Field | Observed | Notes |
|---|---|---|
| `position` | 1, 2, 3 | 1-based; head = lowest. |
| `state` | `AWAITING_CHECKS` | Enum also includes `QUEUED`/`MERGEABLE`/`UNMERGEABLE`/`LOCKED` (not seen live; synthetic fixture covers `UNMERGEABLE`). |
| `enqueuedAt` | ISO-8601 `…Z` | Parse to epoch for `oldest_wait`. |
| `estimatedTimeToMerge` | 1981, 2446, 2911 (secs) | **Grows with queue position** — it's "time until THIS PR merges" (queue-aware), not per-check remaining. Use as the `eta_seconds` shown on the entry; do not treat as build-progress. |
| `headCommit.oid` | distinct per entry | This queue tests **one PR per speculative commit** (batch size 1), so `batch_head_oid` is unique per PR here. The "batch = PRs sharing oid" model still holds — batches are simply singletons in this config. |
| `statusCheckRollup.state` | `PENDING` | Maps: SUCCESS/EXPECTED→passing, FAILURE/ERROR→failing, PENDING/IN_PROGRESS/QUEUED→pending. Can be **null** (synthetic `UNMERGEABLE` entry) → `group_checks=None`. |
| `contexts.totalCount` | 31, 31, 4 | Denominator for progress. |
| `contexts.nodes[].__typename` | all `CheckRun` (live) | Can also be `StatusContext`. |
| CheckRun `status` | 14 `COMPLETED`, 17 `IN_PROGRESS` | **This is the progress source.** 14/31 ≈ 45% — gives the Plotroom-style "47%" the design calls for. |

## Decision: `building.percent` source (resolves the plan's open item)

GitHub exposes **no passed-count**, but it exposes **per-context status**, so progress = completed ÷ total:

- `checks_total` = `statusCheckRollup.contexts.totalCount`.
- `checks_done` = count of contexts that are terminal:
  - `CheckRun` with `status == "COMPLETED"`, **or**
  - `StatusContext` with `state` in `{SUCCESS, FAILURE, ERROR, EXPECTED}`.
- `percent = round(100 * checks_done / checks_total)` when both present, else `None`.

**Plan amendment:** Task 2's `build_merge_queue_query` must fetch `contexts.nodes` (not just `totalCount`), and `parse_merge_queue_response` must compute `checks_done` as above (the plan's stub had `checks_done=None`). Task 7's fixture/asserts updated to match. All other plan tasks are unchanged.
