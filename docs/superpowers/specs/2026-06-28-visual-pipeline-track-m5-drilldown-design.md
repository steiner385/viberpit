# M-Track-5: Per-Check Drill-Down — Design

**Date:** 2026-06-28
**Status:** Draft (design); pending user review → implementation plan
**Program:** Visual Pipeline Track — see
`docs/superpowers/specs/2026-06-27-visual-pipeline-track-overview-design.md`. Fifth milestone,
additive over M-Track-1 (`…-m1-design.md`, PR #333) and M-Track-2 (`…-m2-design.md`, deploy nodes).

---

## 1. Goal

M-Track-1 renders each PR as a horizontal **stage track** (CI → Queue → Merged → deploy nodes). This
milestone brings Plotroom's signature **drill-down**: clicking a PR row expands, **inline**, to reveal
the individual CI checks behind the aggregate `CI` node — Plotroom's two grouped sections:

- **PR CHECKS (HEAD COMMIT)** — the checks on the PR's own head commit.
- **MERGE GROUP BUILD** — the checks GitHub runs on the merge-queue commit for this PR (when it is in
  the queue).

Each check renders as a row: **name + horizontal duration/progress bar + status** (done ✓ / running /
failed ✗ / overdue), plus a per-group **"CI cost this run"** summary line (total wall-time + estimated
runner cost). This is **full parity** with the Plotroom detail panel (scope confirmed during
brainstorming, 2026-06-28).

## 2. Scope

**In (full parity, one milestone):**
- Both check groups (head commit + merge group), grouped by `workflowName`.
- Per-check duration bar, status, required flag, and **overdue** annotation (actual elapsed > expected).
- Per-group **"CI cost this run: Nm (~$X)"** line.
- **Inline** expand/collapse under the PR row (confirmed interaction choice), reusing `PipelineBoard`'s
  existing `collapsed` state pattern.

**Non-goals:**
- **No new poller.** Both groups ride the **existing** eta-checks query (`build_eta_checks_query`),
  extended with one `mergeQueueEntry` sub-selection — no extra GitHub round-trip.
- **No re-fetch on expand.** `checks_detail` ships in the existing `GET /api/prs` payload; expanding is
  pure client state. (Trade-off: detail is as fresh as the last poll, consistent with the rest of the
  board. Documented; not silently implied as live.)
- **No cost configuration surface.** The runner per-minute rate is a single documented constant
  (constitution VI — no parallel config of a thing GitHub already knows). See §3.4.
- **No re-derivation of stage/CI-node state from `checks_detail`.** The aggregate `CI` node state stays
  owned by the existing classifier (constitution VI); `checks_detail` is presentation-only.

## 3. Architecture — Backend

### 3.1 Merge-group checks ride the existing eta-checks query

`server/github/pr_status.py`, `build_eta_checks_query` (the per-PR query the population poller already
runs). Add a `mergeQueueEntry` block alongside the existing head-commit `commits(last:1)` block:

```
mergeQueueEntry {
  estimatedTimeToMerge
  headCommit { statusCheckRollup { contexts(first: 100) {
    nodes {
      __typename
      ... on CheckRun { name status conclusion startedAt completedAt
                        checkSuite { workflowRun { workflow { name } } } }
      ... on StatusContext { context state }
    } } } }
}
```

**Verified against the live GitHub GraphQL schema (probed 2026-06-28 via `gh api graphql`):**
`PullRequest.mergeQueueEntry` exists; `MergeQueueEntry.headCommit` is a `Commit`; `Commit` carries the
same `statusCheckRollup { contexts }` shape already parsed for the PR head; `estimatedTimeToMerge` is an
`Int`. The merge-group `CheckRun` has **no** `isRequired(pullRequestNumber:)` arg (that arg is
PR-scoped); merge-group checks are reported without a required flag (rendered required=false).

### 3.2 `parse_eta_checks_response` returns grouped check rows

Today it returns `dict[(repo,number)] → list[check dict]` (head only). Generalize the return to carry
**both groups** and the queue ETA:

```
dict[(repo, number)] → {
  "head":  list[check dict],          # existing parse, unchanged
  "merge": list[check dict],          # from mergeQueueEntry.headCommit (empty when not enqueued)
  "merge_eta_seconds": int | None,    # estimatedTimeToMerge (minutes from GH) × 60, or None
}
```

The per-check dict shape is unchanged (`{name, status, conclusion, started_at, completed_at, isRequired,
workflowName, event}`); merge-group rows set `event="merge_group"`, `isRequired=False`. The existing ETA
estimator consumer (`pr_population.py`) reads `["head"]` — a one-line change, behavior preserved.

### 3.3 `CheckRow` + `CheckGroup` serializable structs

New frozen dataclasses in `server/github/pr_status.py`:

```
CheckRow:   name, workflow, status, conclusion,
            started_at, completed_at, duration_seconds (int|None),
            required (bool), overdue (bool)
CheckGroup: kind ("head" | "merge"), checks: tuple[CheckRow, ...],
            total_seconds (int), cost_usd (float | None)
```

`duration_seconds` = `completed_at − started_at` when both present; for a running check, `now −
started_at` (elapsed). Legacy `StatusContext` rows (no timestamps) → `None`. `overdue` = the check is
still running **and** elapsed > its expected duration (§3.4).

### 3.4 Cost & overdue derivation reuse the estimator

`server/pipeline/estimator.py` already holds per-check **expected durations** (the basis for PR ETA).
Reuse it for both overdue and cost; do **not** add a parallel timing source (constitution VI).

- **overdue**: running check whose elapsed exceeds its expected duration from the estimator's history.
- **`CheckGroup.total_seconds`**: sum of `duration_seconds` over the group (the "32m" figure).
- **`CheckGroup.cost_usd`**: `billed_minutes × RUNNER_RATE_USD_PER_MIN`, where `billed_minutes` =
  `ceil(total_seconds / 60)` and `RUNNER_RATE_USD_PER_MIN` is a **single documented constant**
  (default `0.008`, GitHub-hosted Linux 2-core list price as of 2026-06). **Assumption, flagged:** we
  cannot know runner OS/size per check from this query, so cost is an estimate for standard Linux
  runners; the `~$` prefix in the UI signals approximation (matching Plotroom). `None` when the group
  has no measured durations.

### 3.5 `PullRequestInfo` gains `check_groups`

Add `check_groups: tuple[CheckGroup, ...] = ()` — additive, default `()`. Population poller
(`pr_population.py`) already computes `checks_by_pr`; build `CheckGroup`s from it and attach to the PR
instead of discarding. `to_dict`/`from_dict` updated additively.

> **Dual-serializer / exact-shape traps (project memory):** a new `PullRequestInfo` wire field must be
> added to `PullRequestInfo.to_dict` **and** `from_dict`, and asserted on a **real** `GET /api/prs`
> response — `_record_to_dict` (`server/api/sessions.py`) builds the PR list via `p.to_dict()`, so it
> rides along, but the exact-dict equality test (`test_sessions_pr_field.py`) and any
> `PullRequestInfo.to_dict` round-trip test must be updated. (Memories:
> `pullrequestinfo-todict-exact-shape-test`, `session-record-dual-serializer`.)

### 3.6 No stage-cursor change

`server/pipeline/track.py` and `place_pr_on_track` are **untouched**. `check_groups` is presentation
detail layered beside the existing node-state machinery; the aggregate `CI` node still derives from the
existing rollup classifier. This keeps the single source of truth for stage state intact.

## 4. Architecture — Frontend

### 4.1 Types

`web/src/api/types.ts`: add `CheckRow`, `CheckGroup`, and `check_groups?: CheckGroup[]` on
`PipelinePr`. `NodeState`-style status union reused where possible.

### 4.2 Inline expandable PR row

`web/src/components/PipelineView.tsx` (`PipelineBoard`): each PR row gains a disclosure affordance
(chevron). Expanded ids live in the **existing** `collapsed`-style `Record<string,boolean>` state
(rename intent: a generic `expanded` map keyed by `${repo}#${number}`), so we reuse the established
pattern rather than adding a second toggling mechanism. Expanding renders the new detail panel; the row
itself is unchanged. Clicking the chevron must `stopPropagation` so it doesn't also fire the existing
row → open-session navigation.

### 4.3 `CheckDrillDown` + `CheckRowList` components

New `web/src/components/CheckDrillDown.tsx`:
- Renders one **group section** per `CheckGroup` with a Plotroom-style header
  (`MERGE GROUP BUILD` / `PR CHECKS (HEAD COMMIT)`) and the `CI cost this run: Nm (~$X)` line.
- Within a group, `CheckRowList` renders rows sorted by workflow then name: **name** (left), a
  **horizontal duration bar**, and **duration + status glyph** (right).
- The bar's fill color reuses `StageTrack`'s state→color vocabulary (green done / accent running / red
  failed / amber overdue) — **extract that mapping into a shared `lib/checkState.ts`** so the palette is
  not forked (constitution VI; the colors currently live inline in `StageTrack.nodeCircleClass`). Bar
  fill fraction = elapsed / expected (clamped 0–1; full + amber when overdue).

### 4.4 No new fetch

`check_groups` arrives in the existing `useAllPrs()` payload; the detail panel reads from the PR object
already in hand. Empty/absent `check_groups` → row simply has no expandable detail (graceful).

## 5. Data flow

```
build_eta_checks_query  (+ mergeQueueEntry block)        — one query, no extra round-trip
  → parse_eta_checks_response → {head, merge, merge_eta_seconds}
  → pr_population: build CheckGroup[]  (duration, overdue, cost via estimator)
  → PullRequestInfo.check_groups
  → to_dict → GET /api/prs → PipelinePr.check_groups
  → PipelineBoard expandable row → CheckDrillDown → CheckRowList
```

## 6. Error handling & edge cases

- **PR not in merge queue** (`mergeQueueEntry` null) → `merge` group empty → only the head group renders.
- **Merge queue disabled on the repo** → same as above; never blank, never errors.
- **Running check** (null `completed_at`) → bar shows elapsed-vs-expected; status = running; eligible for
  overdue. **`now` source:** use `server/time_iso.py` (memory: `naive-utc-isoformat-0s-ago` — never bare
  `utcnow().isoformat()`); duration math compares timezone-aware instants.
- **Legacy `StatusContext`** (no timestamps) → `duration_seconds=None`, no bar fill, status from `state`.
- **No expected duration in estimator history** (new check) → `overdue=False`, bar shows elapsed with no
  expected target (indeterminate style); never falsely flags overdue.
- **`contexts` cap (first:100)** — bounded; very large check sets truncate. `log`/document the cap; do
  not imply full coverage (working norm: no silent caps).
- **Cost `None`** when the group has zero measured durations → render `Nm` omitted / `~$—`, not `$0`.

## 7. Testing (TDD)

**Backend**
- `build_eta_checks_query` emits the `mergeQueueEntry.headCommit.statusCheckRollup` block (string assert).
- `parse_eta_checks_response` returns `{head, merge, merge_eta_seconds}`; merge rows tagged
  `event="merge_group"`; null `mergeQueueEntry` → empty merge group; legacy StatusContext handled.
- `CheckRow` duration: completed (both ts), running (elapsed via injected `now`), legacy (None).
- `overdue`: running + elapsed > expected → True; no history → False; completed → False.
- `CheckGroup` `total_seconds` sum; `cost_usd = ceil(total/60) × RATE`; `None` on empty.
- `PullRequestInfo` `to_dict`/`from_dict` round-trip incl. `check_groups`; **real** `GET /api/prs`
  carries `check_groups`; update `test_sessions_pr_field.py` exact-shape assert.
- ETA estimator consumer still reads `["head"]` — existing ETA tests stay green.
- Full backend suite: no new failures vs `origin/master` (baseline-diff technique, memory
  `worktree-fullsuite-baseline-diff`).

**Frontend (vitest)**
- `CheckRowList`: N rows, correct state→color, duration text, overdue amber, running vs done glyph.
- `CheckDrillDown`: two group headers, cost line format (`32m (~$0.25)`), merge group hidden when empty.
- `PipelineBoard`: chevron expands/collapses; chevron click does not trigger row navigation
  (`stopPropagation`); absent `check_groups` → no chevron/panel.
- Shared `lib/checkState.ts` palette: `StageTrack` still renders identical colors after extraction
  (no visual regression; existing `StageTrack.test.tsx` green).
- Full `vitest run` + `tsc -b --force` + `eslint --max-warnings 0` green.

## 8. Files touched (summary)

**Modified**
- `server/github/pr_status.py` — `build_eta_checks_query` (`mergeQueueEntry`), `parse_eta_checks_response`
  (grouped return), new `CheckRow`/`CheckGroup`, `PullRequestInfo.check_groups` + serializers.
- `server/github/pr_population.py` — build `CheckGroup[]` (duration/overdue/cost), attach to PR; ETA
  consumer reads `["head"]`.
- `server/pipeline/estimator.py` — expose expected-duration lookup for overdue/cost (reuse, no new model).
- `server/api/sessions.py` — verify `check_groups` rides `_record_to_dict` on a real response (test only).
- `web/src/api/types.ts` — `CheckRow`, `CheckGroup`, `PipelinePr.check_groups`.
- `web/src/components/PipelineView.tsx` — inline expand affordance + state.
- `web/src/components/StageTrack.tsx` — consume extracted shared palette.

**New**
- `web/src/components/CheckDrillDown.tsx` (+ `CheckRowList`).
- `web/src/lib/checkState.ts` — shared state→color/label vocabulary.
- Tests for each of the above.

## 9. Risks

- **GraphQL field drift** (`mergeQueueEntry`, `estimatedTimeToMerge`): mitigated by the §3.1
  verify-against-live-schema probe (done 2026-06-28).
- **Cost accuracy**: runner OS/size unknown from this query → cost is a flagged estimate (`~$`), single
  documented constant; never presented as billed truth.
- **Dual serializer / exact-shape tests**: `check_groups` added to `PullRequestInfo.to_dict` — assert on
  real responses; update exact-dict PR test (memories above).
- **Payload size**: `check_groups` enlarges `GET /api/prs`. Bounded by `first:100` per group; acceptable
  for the repo/PR counts in scope. Revisit (lazy per-PR detail endpoint) only if payloads bite.
- **`now`/timezone correctness**: duration/overdue math must use `time_iso` aware instants, not naive
  `utcnow` (memory `naive-utc-isoformat-0s-ago`).
