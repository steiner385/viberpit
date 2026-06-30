# M-Track-3: Stage Summary Tiles (toggle filters) + Session-Inclusive Repos — Design

**Date:** 2026-06-30
**Status:** Approved (design); pending user review → implementation plan
**Program:** Visual Pipeline Track — see
`docs/superpowers/specs/2026-06-27-visual-pipeline-track-overview-design.md`. The summary-tiles part is
the planned M-Track-3 ("Summary tiles … pure frontend over data already flowing"), extended with
click-to-filter (Plotroom behaviour) and a small backend change so archived-session repos appear.

---

## 1. Goal

Two related Pipeline-board enhancements:

1. **Per-stage metric tiles that toggle as filters.** A row of boxes above the board, one per pipeline
   stage (CI · Ready · Queue · Deploy · Merged), each showing a count of PRs currently at that stage.
   Clicking a box toggles it; the board filters to the **union** of selected stages. Just like Plotroom.
2. **Repos with related sessions (live or archived) appear**, even when they have no *open* PR — their
   merged PRs (already rendered today) keep the repo on the board, filterable via the Merged tile.

## 2. Background (verified against current code + live data)

- `/api/prs` **already returns merged PRs** (live: 100 merged + 3 open). The frontend does **not** filter
  by state — it renders all states. Merged PRs already show.
- The board only shows **2 repos** because the population poller watches only repos of **live** sessions:
  `orchestrator.set_repos_provider(self._pr_repos)` and `_pr_repos()` reads in-memory `self._records`.
  Archived sessions persist as `DbSession` rows (status='archived') but are **not** in `_records`
  (orchestrator.py: "archived sessions are NOT in _records"), so their repos are never watched → never
  polled → never on the board.
- The per-PR track position is already computed: `PipelinePr.node_states` (from `place_pr_on_track`)
  marks each node `done`/`active`/`failed`/`pending`. The PR's *current stage* = its `active` node (or
  the furthest `done` node when none is active, e.g. a fully-merged PR).

## 3. Feature 1 — Stage tiles (frontend only)

### 3.1 Bucketing rule (the single source of "what stage is this PR at")
New pure helper `web/src/session/stageBucket.ts`:
```
type StageBucket = 'ci' | 'ready' | 'queue' | 'deploy' | 'merged'
stageBucketOf(pr: PipelinePr): StageBucket | null
```
Derivation, using `pr.node_states` + the repo's `StageModel` node kinds:
- Find the PR's **current node** = the `active` node; if none active, the **last `done`** node in model
  order; if neither, `null` (no track / skeleton).
- Map that node's `kind` to a bucket: `ci→ci`, `gate→ready`, `queue→queue`, `deploy→deploy`,
  `merged→merged`. A node id like `deploy:<env>` has kind `deploy` → the **Deploy** bucket.
- Fallback when `node_states`/model absent: map the flat `pr.stage` string
  (`ci→ci`, `ready→ready`, `parked→ci`, `queue→queue`, `merged→merged`). `parked` folds into CI (it is a
  pre-CI holding state; no separate tile — YAGNI).

Buckets are a **fixed ordered set**: `['ci','ready','queue','deploy','merged']`. Labels:
`CI · Ready · Queue · Deploy · Merged`.

### 3.2 `StageTiles` component
`web/src/components/StageTiles.tsx`:
- Props: `prs: PipelinePr[]`, `active: Set<StageBucket>`, `onToggle(bucket)`.
- Computes `counts` = number of PRs per bucket via `stageBucketOf`. Renders one box per bucket in the
  fixed order, each: label + count, coloured by the shared stage palette (`lib/checkState.ts`
  vocabulary). A box with count `0` renders **disabled** (not clickable, muted). A box in `active`
  renders **highlighted** (ring/filled).
- `data-testid={`stage-tile-${bucket}`}`, `aria-pressed={active.has(bucket)}`.

### 3.3 Filter wiring
- `active: Set<StageBucket>` state lives in `PipelineView` (the data owner), passed to both `StageTiles`
  and `PipelineBoard`.
- `onToggle(bucket)`: add/remove from the set (multi-select). Empty set = **no filter** (show all —
  today's behaviour).
- `PipelineBoard` filters its PR list: when `active.size > 0`, keep only PRs with
  `stageBucketOf(pr) ∈ active`. Repo groups left with **zero** PRs after filtering are **not rendered**
  (they reappear when the filter clears). The existing repo grouping/sorting is otherwise unchanged.
- The tiles count from the **unfiltered** PR list (counts are stable regardless of active filter, so you
  can see all bucket sizes while a filter is on).

## 4. Feature 2 — Session-inclusive repos (backend)

### 4.1 Widen the watched-repo set
`server/sessions/orchestrator.py:_pr_repos()` currently returns
`sorted({r.repo for r in self._records.values() if r.repo})` (live sessions only). Change it to return
the distinct repos across **all persisted sessions** — live + archived — by querying the `DbSession`
table for distinct non-null `repo` (any status that is not hard-deleted; archived rows are kept).

- Because the population poller calls the repos-provider every refresh (~75s) and the provider may be
  async (`Callable[[], list[str]] | Callable[[], Awaitable[list[str]]]`), `_pr_repos` becomes **async**
  and unions: in-memory `_records` repos ∪ `SELECT DISTINCT repo FROM sessions WHERE repo IS NOT NULL`.
  (In-memory union keeps brand-new sessions that may not be flushed yet.)
- Effect: an archived-session repo is watched → its open + merged-within-7d PRs populate → the repo
  appears on the board with its merged PRs, filterable by the Merged tile.

### 4.2 What stays the same
- `retention_days = 7` is **unchanged** (user decision): a repo whose only PR merged >7 days ago still
  won't appear. Documented, accepted.
- No new "empty-track / session-only" repo row type — every shown repo has ≥1 PR (open or
  merged-in-window). This feature only widens *which repos are watched*.

## 5. Data flow

```
DbSession (live + archived) ──_pr_repos()(async)──▶ population watch set
   └─ population polls each repo → open + merged(≤7d) PRs
      └─ all_prs() → /api/prs (merged already included)
         └─ useAllPrs() → PipelineView
              ├─ StageTiles: counts per bucket (stageBucketOf), toggle → active:Set
              └─ PipelineBoard: filter PRs by active set; group by repo; hide empty groups
```

## 6. Error handling & edge cases

- **No active filter** (empty set) → board identical to today (all PRs, all repos with PRs).
- **A bucket with 0 PRs** → tile disabled; clicking is a no-op.
- **PR with no `node_states`/model** (skeleton) → `stageBucketOf` falls back to `pr.stage`; if still
  unknown → `null`, excluded from every bucket count and hidden under any active filter (never crashes).
- **Archived repo with no in-window PRs** → watched but population returns 0 → simply absent (same as a
  live repo with no PRs). Never a blank repo group.
- **DB query cost** — distinct-repo over the sessions table runs once per ~75s poll; trivially cheap and
  indexed-scan small. No per-PR work added.
- **Async provider** — `_pr_repos` returning a coroutine must be awaited by the poller's existing
  async-provider path (already supported); confirm the provider invocation awaits.

## 7. Testing (TDD)

**Frontend (vitest)**
- `stageBucketOf`: active-node mapping for each kind (ci/gate/queue/deploy/merged); last-done fallback
  (fully-merged → merged); flat-`stage` fallback when no node_states; `parked→ci`; unknown → null.
- `StageTiles`: counts per bucket from a PR list; 0-count tile disabled; active tile highlighted
  (`aria-pressed`); toggle calls `onToggle`.
- `PipelineView`/`PipelineBoard`: empty active set → all rendered; active {queue} → only queue PRs and
  only repos with a queue PR; multi-select {queue,merged} → union; clearing restores.
- Full `vitest run` + `tsc -b --force` + `eslint --max-warnings 0`.

**Backend (pytest)**
- `_pr_repos` (async) unions live `_records` repos with archived `DbSession` repos; distinct + sorted;
  null repos excluded; a repo present only via an archived session is included.
- The poller awaits the async provider and watches the unioned set (existing population/orchestrator
  tests stay green; add one asserting an archived-only repo enters the watch set).
- Full server suite: no new failures vs `origin/master` baseline. **Because this changes `_pr_repos`'s
  signature (sync→async), grep ALL call sites and run the WHOLE `server/tests/`** (memory:
  `signature-change-needs-full-suite`).

## 8. Files touched (summary)

**New**
- `web/src/session/stageBucket.ts` — `stageBucketOf` + `StageBucket` type + ordered bucket list/labels.
- `web/src/components/StageTiles.tsx`.
- Tests for both.

**Modified**
- `web/src/components/PipelineView.tsx` — `active:Set` state; render `StageTiles`; pass filter to board;
  filter PRs + hide empty repo groups.
- `server/sessions/orchestrator.py` — `_pr_repos` async, unions archived-session repos from `DbSession`.
- Backend test(s) for the widened watch set.

## 9. Risks

- **`_pr_repos` sync→async signature change** — every caller + the poller's provider-await path must
  handle the coroutine. Grep all call sites; whole-suite run is the gate (the M6 lesson:
  `signature-change-needs-full-suite`).
- **Bucket rule divergence from the rendered track** — `stageBucketOf` must use the SAME `node_states`
  the `StageTrack` renders, so a tile's count matches what the user sees lit on the track. Test the
  active-node derivation against representative node_states.
- **Watch-set growth** — unioning all archived repos could grow the population poll fan-out for a user
  with many archived repos; bounded by distinct repo count (small), and each repo is one GraphQL call
  already batched by the poller. Acceptable; note if a user has hundreds of archived repos.
