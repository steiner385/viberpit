# 003 — GitHub-Native Planning: Projects v2 Integration for the SDLC/PM Suite

**Status:** approved design, pending Phase 0 verification
**Date:** 2026-09-03
**Origin:** brainstorming session; supersedes the freestanding-Initiative model of
`docs/product/features/product-management-layer.md` Phase 4 (cross-project initiatives) where they
conflict. Phases 1–3 of the PM layer are unaffected and are load-bearing prerequisites.

## 1. Problem & goals

The SDLC (`Initiative` stage machine + artifact chain) and PM (`Project`/`Feature`/`WorkItem`/
triage) subsystems exist but do little day-to-day work. Meanwhile real coordination happens in
GitHub issue bodies: dependencies are written three inconsistent ways (native blocked-by
relationships, `Depends on #N` prose, epic sub-issue/task-list ordering), initiatives like epic
#945 exist twice (an unused DB row and a hand-written epic issue), and nothing decides which of
several parallel initiatives' ready work agents should pull.

Goals, all first-class:
1. **Route agent work correctly** — dependency-aware, initiative-aware, priority-aware selection.
2. **Portfolio visibility** — one place to see all parallel initiatives across products, their
   stages, blockers, and pending human decisions.
3. **Dependency correctness** — one canonical, queryable dependency graph.
4. **Planning/decomposition flow** — the intake→design→plan lifecycle *produces* the tracked
   epics/phases/issues instead of describing them in prose.

## 2. Decisions of record

| # | Decision |
|---|----------|
| D1 | **GitHub-native primitives are truth** for planning state: native blocked-by dependencies, sub-issue epics, Projects v2 fields. ccswitch mirrors, annotates, and renders. |
| D2 | **Initiative = epic issue.** The `Initiative` row becomes a mirror/overlay keyed to an epic (`repo#number`), like `WorkItem`. Stage machine, artifact chain, and session launching remain ccswitch overlay. |
| D3 | **One Project v2 per product** (cc-account-switcher, KinDash, infra, …); ccswitch aggregates the cross-product portfolio view. |
| D4 | **Fully automatic write posture.** A reconciler keeps GitHub consistent with decisions already made in ccswitch (triage acceptance, stage transitions, session activity). Judgment lives in the decision layer's existing human gates, not in per-write approval. |
| D5 | **Global priority queue** across all products/initiatives: rank = Score (RICE-informed) + dependency readiness. Agents always pull the top eligible item anywhere. |
| D6 | **Evolve in place, phased** (mirror → reconciler/migration → consumers → consolidation). No parallel new subsystem. |

## 3. Architecture & ownership model

```
GitHub (canonical)                ccswitch mirror (read model)        ccswitch decisions (process)
──────────────────                ────────────────────────────        ────────────────────────────
Epic issues + sub-issues     ←→   Initiative rows (overlay)           Stage machine, artifact chain,
Native blocked-by graph      ←→   WorkItemDep (new)                   session launching
Projects v2 (per product)    ←→   GhProject/GhProjectItem (new)       Triage (RICE) → Score
Issue/PR state               ←→   WorkItem (exists)                   Backlog runner queue
```

- **Read path:** `pm_sync` grows three reads per product — Project v2 items+fields, native
  dependency edges, sub-issue relationships — landing in mirror tables. Polling (user-account
  Projects have no webhooks); the existing watermark pattern applies.
- **Write path:** a new reconciler automation converges GitHub toward the decision layer's desired
  state via diffed, batched, journaled GraphQL mutations (§6).

**Ownership matrix** — prevents reconcile ping-pong:

| State | Truth | ccswitch writes when… | Human GitHub edit… |
|---|---|---|---|
| Dependency edges | GitHub native | compiling prose/order → native links | wins, back-syncs |
| Epic structure (sub-issues) | GitHub | plan materialization creates them | wins, back-syncs |
| Project `Status` | GitHub field | session activity / PR merge changes it | wins |
| `Score` field | GitHub field | triage acceptance recomputes RICE | wins, back-fills overlay |
| `Stage` field | GitHub field | stage-machine transition | wins, drives stage machine |
| Issue body/title/labels | GitHub | never (one-time migration excepted) | n/a |

Rule: the reconciler pushes a value only when the ccswitch-side decision timestamp is strictly
newer than the mirrored value's `gh_updated_at`. Its own writes are echo-suppressed (§6) so the
next poll does not re-ingest them as human edits. Direct GitHub edits therefore always win.

**What dies (constitution VI):** the Initiative table's freestanding identity; prose-dependency
*authoring* as a convention (the parser survives as a compiler input and dispatch-time safety
net); eventually the `in-progress`/`needs-human` labels (replaced by `Status`).

## 4. GitHub schema & conventions

Custom fields per product Project — deliberately minimal:

| Field | Type | Written by | Meaning |
|---|---|---|---|
| `Status` | built-in single-select | reconciler + humans | `Backlog · Ready · In progress · Blocked · Needs human · Done` |
| `Score` | number | reconciler | global-queue rank input; higher = sooner |
| `Stage` | single-select | reconciler | initiative lifecycle stage; meaningful on epic items only |

Built-ins carry the rest: **Parent issue** (initiative membership — structural, no custom field),
Repository, Assignees, Linked PRs. No Iteration field in v1 (no sprint cadence exists; fields are
API-creatable later).

Conventions:
- **Initiative** = an issue with sub-issues, in its product's Project, with a `Stage`. `Epic: …`
  title kept for humans; epic-ness is structural.
- **Phase/task order** = explicit sibling blocked-by links, compiled from list order at
  materialization time. List order alone is never trusted afterward.
- **Cross-repo/product deps** = native blocked-by across repos, rolled up to `ProjectEdge` for the
  portfolio view.
- Views cannot be created via API: one-time manual view setup per Project, documented as a runbook.

## 5. ccswitch data model

All new columns nullable (picked up by `_reconcile_columns` on both dialects). New tables need
Alembic revisions plus entries in the two hand-maintained Postgres-suite lists
(`_REQUIRED_EXTRAS`, and `RECONCILE_EXEMPT` where applicable).

New mirror tables (dumb read models — no state machines, no invariants beyond FKs):
- **`GhProject`** — `project_id` FK (ccswitch product), `gh_project_number`, `gh_node_id`,
  `title`, field-definition cache (JSON: name → gh field/option node IDs), `watermark`.
- **`GhProjectItem`** — `gh_project_id` FK, `work_item_id` FK, `gh_item_node_id`, `status`,
  `score`, `stage`, `gh_updated_at`. Substrate of the queue.
- **`WorkItemDep`** — `src_work_item_id`, `dst_work_item_id` (nullable) + `dst_ref` text for
  unmirrored cross-repo targets, `source` (`native | prose | order` provenance), `active`.
- **`GhWriteJournal`** — target node ID, field, old→new values, mutation ID, timestamp,
  `initiated_by` (originating decision), `applied`. Serves echo suppression, audit, and rollback.

Changed tables:
- **`WorkItem`** — `parent_work_item_id` (sub-issue mirror), `is_epic` (cached derivation).
- **`Initiative`** — `epic_repo` + `epic_number` overlay key. An Initiative without a linked epic
  is legal only in pre-build stages (the epic is created at plan materialization).
- **Triage** — unchanged; acceptance additionally enqueues reconciler writes (Score/Status).

## 6. Reconciler

Home: `server/automations/gh_reconcile.py` — standard control-loop registration, status card,
config surface. One loop iterating over products (single point for scheduling and rate budget).

Converge cycle, per product:
1. **Freshness gate:** mirror must be fresher than the newest pending decision; otherwise trigger
   a sync and skip the cycle. Never diff against a stale mirror.
2. **Desired state** from the decision layer only: dependency compilation (prose markers +
   materialization list order → missing native edges); field values (Score from accepted RICE,
   Stage from the stage machine, Status from live signals — active session → `In progress`,
   merged closing PR → `Done`, open blockers → `Blocked`, human-gated decisions →
   `Needs human`); membership (open issues in product repos not yet in the Project → add).
3. **Diff** vs. mirror, applying the ownership timestamp rule (§3).
4. **Apply** as aliased GraphQL mutation batches (~10–20/document), write-ahead journaled: the
   journal row exists before the call, so a crash mid-batch leaves a verifiable entry, never an
   untracked mutation.
5. **Echo suppression:** `pm_sync` compares inbound changes against recent journal entries
   (node ID + value + time window); matches update the mirror silently.

Safety rails:
- **Fleet leader-gating:** runs on the primary host only; every cycle is fenced on
  `hosts.sha == origin/master`. A stale daemon must refuse to write to GitHub (rocky rule).
- **Kill switch + dry-run:** `CCSWITCH_GH_RECONCILE_ENABLED` (default off until Phase B ships);
  `dry_run` computes and journals diffs with `applied=false`.
- **Blast-radius cap:** max mutations per cycle (config, default ~50); exceeding halts loudly on
  the status card. Caps are logged, never silent.
- **Rate budget:** GraphQL point tracking with backoff, shared with `pm_sync` accounting.
- **No blind retry:** a mutation failing twice parks as `needs_human` in the journal.

**One-time migration** = a `migrate` mode of this same machinery: compile all open-issue prose
deps to native; restructure existing epics (task lists → sub-issues + sibling blocked-by); create
per-product Projects (fields via API, views by hand); backfill items/fields. Dry-run, review
journal, apply. Historical prose stays in bodies.

## 7. Consumers

### 7a. Backlog runner — global priority queue

Candidate source flips from live label-search to the mirror:

```
eligible = GhProjectItem where status = Ready
         ∧ no active WorkItemDep edge with an open blocker
         ∧ not assigned to a live session
ordered by score DESC, then epic phase order, then age   — across all products
```

- `backlog_deps.py` eligibility becomes a mirror query. The prose parser and epic-order logic
  move into the reconciler as compiler inputs and are deleted from the runner's hot path in the
  same PR.
- Retained safety net: re-verify the top candidate's blockers against live GitHub immediately
  before dispatch (one query; fails closed on error).
- `respect_dependencies` config keeps its meaning; `blocked` counts come from the graph.
- Status card gains queue provenance ("picked X (score S, product P) over Y…").

### 7b. Plan materialization

New terminal action of the `plan` stage: **Materialize**. On plan-artifact PR merge, a session:
1. Decomposes the plan into phase/task issues — posted as a reviewable proposal (triage-style
   human gate; decomposition is judgment).
2. On acceptance, mechanically creates epic + sub-issues + sibling blocked-by chain + Project
   items (`Stage=build`, Score seeded from initiative RICE).
3. Links the Initiative row to the epic (overlay key).

Epics created GitHub-first are **adopted**: the reconciler detects an unlinked epic in a Project
and creates its Initiative overlay row at `stage=build`.

### 7c. Portfolio UI

PM tab becomes the portfolio home; the Initiatives tab merges into it:
- **Portfolio level:** per-product cards — active initiatives with stage, ready/blocked/
  needs-human counts, cross-product dependency warnings.
- **Initiative level:** existing InitiativeTrack, now rendering epic/sub-issue progress and the
  dependency chain from mirror data alongside the artifact chain. Deep-links to the GitHub
  Project for board manipulation — ccswitch does not rebuild a drag-drop board; its value is the
  cross-product lens GitHub cannot render.
- **Queue view:** the global queue via the same query the runner uses — what you see is what
  agents will do.

Deleted with 7a–7c: runner-side dep parsing, label-based candidate search, standalone Initiatives
tab, `in-progress`/`needs-human` label writes (labels cleaned up by migration).

## 8. Phasing

**Phase 0 — verification spike** (constitution III; before any implementation):
1. Auth: can the GitHub App access user-owned Projects v2, or is a PAT with `project` scope
   required? Decide the credential story (App has lost perms before — #902).
2. Confirm mutations: native dependency add/remove, sub-issue add/reorder, aliased batched field
   updates.
3. Delta reads: can Project items be queried by updated-time, or is per-cycle full paging
   required (acceptable at current scale)?
4. Confirm limits (deps/issue, sub-issues/parent, items/Project) don't bind.
Output: findings doc; contradictions reopen the affected sections.

- **Phase A — Mirror (read-only):** `pm_sync` extensions, four new tables, drift-audit report,
  deps rendered in UI. Zero write risk; answers "what blocks X."
- **Phase B — Reconciler + migration:** dry-run on real data, journal review, enable. Ends with
  per-product Projects live, all deps native, Status/Score real.
- **Phase C — Consumers:** runner queue flip (+ same-PR deletions), plan materialization, epic
  adoption.
- **Phase D — Consolidation:** UI merge, label retirement, docs.

**Dogfooding:** this initiative is managed GitHub-first from day one — hand-built epic +
sub-issues + native blocked-by chains. It becomes the first structure Phase B adopts and Phase C
executes.

## 9. Testing

- **Pure cores unit-tested (TDD):** diff engine, dependency compiler, ownership-timestamp rule,
  queue ordering — pure functions over mirror rows; golden tests of (mirror state + decisions) →
  expected mutation batch, no network.
- **Sync layer:** fixture GraphQL payloads (existing `pm_sync` pattern); dedicated
  echo-suppression tests (journal hit → silent mirror update; miss → human edit).
- **Schema:** Alembic revisions; entries in the two Postgres-suite lists; a real
  `CCSWITCH_TEST_PG_URL` run locally (CI skips it).
- **Live verification:** dry-run mode is the integration test — journaled diffs against the real
  repo, reviewed before every first enable.
- **Web:** full vitest suite (not changed-file-only), `tsc --noEmit`, eslint.

## 10. Failure modes

| Failure | Behavior |
|---|---|
| GitHub down / rate-exhausted | Mirror stales; reconciler skips (freshness gate); runner works off last-good queue but pre-dispatch live blocker check fails closed |
| Reconciler bug wants mass-edit | Blast cap halts at N, loud status; journal old→new enables reverting an applied batch |
| Human edits field mid-cycle | Timestamp rule: their edit is newer → delta suppressed, back-syncs next poll |
| Project/field deleted or renamed by hand | Field-cache validation fails → that product halts as `needs_human`; no guessing |
| Dep targets unmirrored repo | `dst_ref` text edge; resolved live at dispatch (retained `_resolve_dep_states` path) |
| Stale fleet daemon | SHA fence refuses the cycle before any read or write |

## 11. Out of scope (v1)

- Iteration/sprint fields and any time-boxed planning.
- Rebuilding a drag-drop board in ccswitch.
- Org migration or any org-only feature (issue types, Projects webhooks).
- Milestones (Projects fields cover the need).
- Automated view creation (API does not support it).
