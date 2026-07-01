# M-Track-10 — Merge-Train Widget (Plotroom-style queue train)

**Date:** 2026-06-30
**Status:** Approved design, pending implementation plan
**Feature area:** 001-ccswitch-web · Pipeline view

## Problem

ccswitch's Pipeline view shows per-PR metro rows (CI → Queue → Merged → QA → Prod),
the summary stat tiles, and a per-check drill-down. It has **no view of the merge
queue as a whole** — which PRs are batched together right now, what's building, what's
blocked by a conflict, what merges next, and how the queue has been performing.

Plotroom (the predecessor CI/CD tool ccswitch is consolidating) renders a per-repo
"queue train" summary that answers all of this at a glance. This feature ports that
widget into ccswitch's existing Pipeline view.

### Reference (Plotroom) — the target

A per-repo block showing:
- a **health line**: `depth 5 · 0.8 trains/hr · 38% batch success · 46 ejects 24h · oldest wait ~1h 36m`
- **segment boxes**: `failing #10823` · `group #10824 47% ~12m left` · `blocked behind conflict #10805 #10799 #10814`
- a **deploy-backlog line**: `1 awaiting QA · 42 awaiting prod`
- a **merges-next line**: `Merges next: #10824 building 47% · ~12m`

## Scope

**In scope:** the per-repo merge-train widget (health line + segment boxes +
deploy-backlog line + merges-next line), including the historical metrics, rendered
at the top of each repo group in the existing Pipeline view.

**Out of scope:** Plotroom's broader app chrome (the Health / Diagnose / Model & Edit /
Insights left-nav and the Classic toggle). ccswitch's existing navigation is unchanged.

## Decisions (from brainstorming)

1. **Fidelity:** full widget including historical metrics (`trains/hr`, batch success %,
   ejects/24h), not just the live tier.
2. **Storage:** SQLite (persistent) — the 24h rolling stats survive a server restart and
   reflect a true window. Follows the existing `HistoryStore` precedent.
3. **Boundary:** the train widget only; no new nav tabs.
4. **History model: Approach A — snapshot + derive-on-read.** Persist a compact queue
   snapshot each poll; compute every metric as a pure function of the snapshot window at
   read time. The snapshots are the single source of truth (constitution VI); metrics are
   recomputable, so a math bug is a code fix, not corrupted data. A retention prune keeps
   the table bounded.

## Architecture

### Data flow

```
PrPopulationPoller sweep (existing, ~15s)
  └─ NEW: query repo-level mergeQueue.entries per watched repo
       └─ write one normalized row per queued PR → SQLite (merge_queue_observation)
            └─ prune rows older than 36h
GET /api/pipeline/trains
  └─ derivation layer reads the 24h window of observations for a repo
       └─ pure functions → MergeTrainState (live segments + rolling stats)
            └─ to_dict()
React: useMergeTrains() (10s refetch)
  └─ MergeTrainWidget rendered atop each repo group in PipelineBoard
```

The capture extends the **existing single poller** (constitution VI — no second poller).
The read path serves from stored/cached state with **no GitHub call per request**, same
pattern as `GET /api/prs`.

### Components & boundaries

| Unit | Purpose | Depends on |
|---|---|---|
| `mergeQueue` GraphQL fragment | Fetch repo-level queue entries in the existing sweep | GitHub GraphQL |
| `merge_queue_observation` (SQLite) | One row per queued PR per poll; the source of truth | SQLAlchemy/SQLite |
| Snapshot store (`record_observation`, `window`, `prune`) | Write/read/prune observations | the table |
| Derivation layer (pure functions) | Snapshot window → `MergeTrainState` | snapshot store (read only) |
| `MergeTrainState` dataclass + `to_dict()` | Wire shape | — |
| `GET /api/pipeline/trains` | Serve `{repo: MergeTrainState}` to the frontend | derivation layer, auth |
| `useMergeTrains()` hook | 10s refetch, mirrors `useAllPrs` cadence | API |
| `MergeTrainWidget.tsx` + `TrainSegment` | Render the widget atop each repo group | hook, `PipelineBoard` |

## The GitHub query — verify first (constitution III)

ccswitch currently reads only **per-PR** `mergeQueueEntry.headCommit.statusCheckRollup`.
The widget needs the **repo-level** queue:

```graphql
repository(owner, name) {
  mergeQueue {
    entries(first: 50) {
      nodes {
        position
        state            # QUEUED | AWAITING_CHECKS | MERGEABLE | UNMERGEABLE | LOCKED
        enqueuedAt
        estimatedTimeToMerge
        headCommit { oid statusCheckRollup { state } }
        pullRequest { number }
      }
    }
  }
}
```

**Plan Task 1 verifies this against the real GitHub API** (field availability, enum
values, whether `entries` exposes batch grouping or whether `headCommit.oid` is the only
batch key) before any code depends on the shape. If a field is missing, metric
definitions adapt — we do not guess.

## Data model

### SQLite: `merge_queue_observation`

One row per queued PR per poll. **All new columns nullable** (dev DB `create_all` never
ALTERs; `init_db` backfills nullable columns only).

| column | type | notes |
|---|---|---|
| `id` | int PK | |
| `repo` | str | nameWithOwner |
| `captured_at` | int (epoch) | poll timestamp |
| `pr_number` | int | |
| `position` | int? | queue position |
| `state` | str? | GitHub enum passthrough |
| `enqueued_at` | int (epoch)? | for oldest-wait |
| `batch_head_oid` | str? | speculative merge commit = **batch identity** |
| `group_checks` | str? | `passing` / `failing` / `pending` |
| `eta_seconds` | int? | from `estimatedTimeToMerge` |

**Retention:** prune rows with `captured_at` older than 36h (margin over the 24h window),
run during the sweep that writes new rows.

### `MergeTrainState` (wire shape, per repo)

```
MergeTrainState
  health: { depth, trains_per_hour, batch_success_pct, ejects_24h, oldest_wait_seconds }
  segments:
    failing:  [pr_number, ...]
    building: { pr_number, percent, eta_seconds } | null   # the active batch
    blocked:  [pr_number, ...]                              # UNMERGEABLE / conflict
  deploy_backlog: { awaiting_qa, awaiting_prod }            # reused from PipelinePr data
  merges_next: { pr_number, percent, eta_seconds } | null   # head of queue
```

## Metric definitions (pure functions over the snapshot window)

**Live (latest snapshot):**
- `depth` = number of entries in the latest snapshot.
- `oldest_wait_seconds` = `now − min(enqueued_at)` across current entries.
- **failing** = entries whose batch (`batch_head_oid`) has `group_checks == failing`.
- **building** = the active batch currently running checks; `percent` from check
  progress / ETA, `eta_seconds` from `estimatedTimeToMerge`.
- **blocked** = entries with `state == UNMERGEABLE` (merge conflict).
- **merges_next** = lowest-`position` entry, with its building % / ETA.
- **deploy_backlog** = counts derived from existing `PipelinePr` deploy state
  (`awaiting QA` / `awaiting prod`) — **no new source of truth.**

**Historical (diff consecutive snapshots across the 24h window):**
- A **batch** = the set of PRs sharing one `batch_head_oid`.
- A PR present in snapshot N−1 and absent in N is an **eject** if it is not merged, a
  **success** if it is merged.
- `ejects_24h` = count of eject events in the last 24h.
- `batch_success_pct` = merged batches ÷ (merged + failed batches) over the window.
- `trains_per_hour` = completed batches ÷ window hours.

## Error handling

- A queue-query failure in a sweep is **fire-and-forget** (like the rest of the sweep):
  keep the last observation, skip this cycle's write, never block the PR poll.
- The derivation layer is resilient to gaps in the snapshot series (missed polls don't
  corrupt stats; they slightly reduce resolution).
- **Empty/idle repo:** when `depth == 0` and there is no activity in the 24h window, the
  widget collapses to nothing — repos without a merge queue render no widget (no noise).
- Read path serves stale-but-labeled data if the latest snapshot is older than expected.

## Testing (TDD)

- **Derivation functions (heaviest coverage):** synthetic snapshot sequences asserting
  eject detection, batch success %, trains/hr, oldest-wait, and each live segment's
  membership. These are pure functions — exhaustive, fast unit tests.
- **Snapshot store:** round-trip write/read and the 36h prune.
- **API:** `MergeTrainState.to_dict()` exact-shape test (matches the existing
  `to_dict` exact-shape discipline).
- **Frontend:** vitest render tests for `MergeTrainWidget` from a fixture
  `MergeTrainState`, including the collapsed empty state.
- Auth: endpoint 401s without a verified identity; pin `CCSWITCH_TAILNET_ALLOWLIST`
  per-test per the project testing notes.

## Open items resolved during planning

- Exact `mergeQueue` field availability (Plan Task 1, against real API).
- Whether `percent` for the building batch comes from check counts or ETA elapsed — pick
  during Task 1 based on what the API actually exposes.
