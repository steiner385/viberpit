# Product Management Layer — Design

**Date:** 2026-08-31
**Status:** Approved design, pre-implementation
**Builds on:** `2026-08-29-ai-native-sdlc-design.md` (Initiative spine, artifact chain, propose-only automations)

## 1. Problem & goal

The SDLC feature gives ccswitch a delivery pipeline (initiative → intent → spec → plan → build),
but no product layer above it: there is no way to onboard an existing project with its backlog and
history, no durable map of what each product *is*, no prioritization, and no model of how projects
relate. Product management currently happens nowhere (or in heads).

**Goal:** make ccswitch a genuinely good product-management home for a multi-project setup:

- Onboard an existing GitHub project — backlog issues, PRs, and history — into a curated product
  model in about an hour, with AI doing the archaeology and a human gating every artifact.
- Encode modern product-ownership conventions: a durable feature/capability map per project
  (separate from delivery work), outcome-driven feature definitions, structured prioritization,
  optional discovery artifacts.
- Model relationships between projects: dependency edges, cross-project initiatives,
  feature-to-feature links, portfolio grouping.
- Keep the model alive continuously: new items are AI-triaged into the map, map drift is detected
  and proposed back to the human. Curation, not data entry.

## 2. Decisions

Settled during brainstorming (2026-08-31):

| Question | Decision |
|---|---|
| Source of truth | **Hybrid, layered.** GitHub owns work items (issues/PRs). Repo markdown owns durable product artifacts (feature docs, SDLC artifacts). ccswitch DB owns only index, overlay (classification/scores), and relationships. |
| Hierarchy | **Project → Feature → Initiative → Issues.** Features are the long-lived capability map ("what the product is"); initiatives are delivery efforts that change features. |
| Onboarding | **AI product archaeology**: sync mirror → AI-synthesized feature map as a reviewable PR → AI-proposed backlog classification. Every step human-gated. |
| Relationships | All four: project dependency edges, cross-project initiatives (Initiative↔repo goes many-to-many), feature-level links, portfolio rollup (trails). |
| PM conventions | Outcome-driven feature definitions; RICE-lite prioritization; optional discovery stage. **No OKR layer** (rejected as ceremony). |
| Ongoing | **Continuous PM loop**: incremental sync + AI triage proposals + drift detection. Propose-only, human-gated — same paradigm as SDLC automations. |
| V1 surfaces | Triage inbox, per-project product board, priority roadmap. Portfolio dashboard trails. |
| Architecture | **Approach A — extend the SDLC spine in place.** New automations follow the established pattern (orchestrator loop + pure engine + prompt builders + injected seams). No parallel subsystem (Principle VI); GitHub Projects v2 rejected as backbone (weak cross-repo primitives, rate-limit-bound). |

## 3. Domain model

Governing rule: every table is exactly one of index, overlay, or relationship — never a second
truth.

### New entities (`server/db/models.py`)

| Entity | Purpose | Truth lives in |
|---|---|---|
| `Project` | slug, name, description, status, portfolio tags, `onboarding_state` | ccswitch DB |
| `ProjectRepo` | project↔repo with `is_primary` (feature docs live in the primary repo); carries sync state: backfill cursor, incremental watermark, `last_synced_at` | ccswitch DB |
| `ProjectEdge` | typed edge: `depends_on` \| `deploys_via` \| `integrates_with`, free-text note; unique (src, dst, kind) | ccswitch DB |
| `Feature` | index row for `docs/product/features/<slug>.md`: project_id, slug, title, lifecycle state, repo_path, commit_sha (null while PR open), pr_number/pr_url | **Repo markdown** (row is a pointer, like `InitiativeArtifact`) |
| `FeatureLink` | feature↔feature edge (`powered_by` \| `integrates_with`), cross-project allowed | ccswitch DB |
| `WorkItem` | read-only mirror of a GitHub issue or PR: repo, number, kind (issue\|pr), title, body, state, labels (JSON), author, timestamps, url, `last_synced_at`; unique (repo, number, kind) | **GitHub** (mirror is disposable, rebuildable by re-sync) |
| — overlay columns on `WorkItem` | the *approved* overlay: `feature_id`, RICE-lite scores (`reach`/`impact`/`effort`/`confidence`, nullable small ints), `triage_state` (`unclassified` \| `proposed` \| `classified` \| `needs_human`) | ccswitch DB |
| `TriageProposal` | AI proposal awaiting the gate: work_item_id, proposed feature (or `no_fit` + suggested name), proposed scores, rationale, confidence, status (`pending`\|`accepted`\|`rejected`), session_id | ccswitch DB (audit trail of AI vs. human decisions) |

The proposal/approved split is the human gate made structural: AI writes `TriageProposal` rows;
only an accept action copies values onto the `WorkItem` overlay. Overlay and proposals key off
(repo, number) identity, not mirror-row identity, so the mirror can be dropped and re-synced
without losing anything ccswitch owns.

### Changes to existing models

- `Initiative`: add `project_id` (nullable FK), `InitiativeRepo` M2M (phase-1 groundwork for
  cross-project initiatives; `Initiative.repo` remains the primary until folded in),
  `InitiativeFeature` M2M (features this effort touches), and the four nullable score columns.
- `INITIATIVE_STAGES`: gains optional `discovery` before `intake`, with a `discovery` artifact
  kind (`docs/sdlc/<slug>/discovery.md`, hypothesis/experiment doc). Initiatives may start at
  `discovery` or `intake`; the engine treats a missing discovery artifact as skipped, never
  blocking.

### Feature doc convention

`docs/product/features/<slug>.md` — YAML frontmatter carries machine-readable index fields (slug,
lifecycle state, links); body follows the outcome-driven template: **Problem · Target users ·
Desired outcome · Success metrics · Non-goals · Notes**, plus an **Evidence** line (file paths,
representative issue/PR numbers). AI fills only what evidence supports; gaps are explicit
`(unknown — needs owner input)` markers. Lifecycle: `proposed → discovery → delivering → live →
sunset`. Sync re-indexes the DB row whenever the merged doc changes. A deleted doc marks its row
sunset-orphaned rather than deleting it, so links never dangle silently.

### Schema mechanics

All new columns nullable (reconcile picks them up on both dialects). Every new model lands with
its `_REQUIRED_EXTRAS` / `RECONCILE_EXEMPT` entries and an Alembic revision chained off the newest
head, in the same commit. Postgres suite run locally (`CCSWITCH_TEST_PG_URL`) before push.

## 4. GitHub sync & ingestion (`pm-sync`)

New automation: `server/automations/pm_sync.py` loop + thin orchestrator seams + `Automation`
config row (`mode: off|on`, cadence). Per onboarded repo, two jobs:

**Work-item mirroring (GitHub → `WorkItem`).** GraphQL exclusively (~100 items/query).
- *Backfill* (onboarding): paginate the full issue + PR history including closed/merged (history
  feeds archaeology), storing a resumption cursor on `ProjectRepo` so interruption resumes rather
  than restarts. Mirror title, body, state, labels, author, timestamps, linked-PR refs.
- *Incremental* (steady state): query by `updated_at` since the watermark. The watermark advances
  **only after a fully successful pass**; failed ticks re-read a few items next time (idempotent
  upserts make that harmless) and never miss one.

**Feature index refresh (repo markdown → `Feature`).** Read `docs/product/features/*.md` from the
local clone at `origin/<default-branch>` via the existing `gitrepos` fetch machinery (no API
cost), parse frontmatter, upsert `Feature` rows. Malformed docs log and skip — they never poison
the index.

**Cadence & limits.** Poll every 5 minutes per project (config-tunable); fine inside the
5,000-point/hr GraphQL budget at personal scale. Webhooks **deferred** (inbound public endpoint =
Principle IV exposure question with no current need); a sync-now trigger (API + UI button) covers
on-demand freshness.

**Failure posture.** Per-repo isolation; errors log to the automation event stream; ticks retry
naturally. No retry queues or dead-letter machinery — watermark + idempotent upserts are
self-healing at this scale.

## 5. Archaeology onboarding

Wizard over an observable state machine on `Project.onboarding_state`:
`registered → syncing → map_proposed → map_merged → classifying → live`. The wizard is a *view*
over state, never authoritative — interruptible and resumable by construction.

- **Step 0 — Register.** New Project form: name, repo(s), primary repo, optional portfolio tags
  and edges. Registration kicks off backfill sync immediately (no AI).
- **Step 1 — Synthesize the feature map** (gate: PR merge). "Propose feature map" spawns an
  archaeology session (existing orchestrator spawn seam, normal account rotation) that reads the
  codebase in the clone, the mirrored backlog (open + closed), and PR history (churn concentration
  is evidence of real capability areas); writes one feature doc per capability with Evidence
  lines; opens **one non-draft PR** with the whole map, no auto-merge. The merge is the curation
  gate; PR review comments are where the human argues with it.
- **Step 2 — Classify the backlog** (gate: triage inbox). Classification sessions process **open**
  items in chunks (~50/session, structured output); closed history served as evidence and is not
  classified. Each item gets a `TriageProposal` (feature, scores, rationale, confidence;
  low-confidence flagged). Queue drained → project is `live`; the continuous loop takes over.

Re-running "Propose feature map" on a live project is legal and produces a *diff* PR against the
existing map — the same path the drift mechanism uses. First dogfood target: ccswitch itself, then
KinDash.

## 6. Continuous triage loop (`pm-triage`)

Pure decision engine (`server/pm/engine.py`, zero I/O) + orchestrator seams + `Automation` row.
Modes `off | propose` only — no `auto` in v1. A tick:

1. **Classify the new.** Open, unclassified `WorkItem`s with no pending proposal are batched into
   classification sessions — **the same code path as onboarding Step 2** (onboarding-classify is
   this loop pointed at a big queue; deliberately no second implementation). Daily session cap
   bounds cost.
2. **Auto-link the mechanical.** PRs referencing an issue via closing keywords inherit that
   issue's feature at sync time — no AI session. Only unlinked PRs go to the classifier.
3. **Detect map drift.** The classifier may answer **`no_fit`** with a suggested new-feature name
   instead of forcing a bad assignment. Accepted `no_fit`s clustering past a threshold (default
   ~5 kindred items) make the engine propose a feature-map update — the archaeology-diff PR path,
   scoped to the unnamed capability. On merge, orphaned items are re-proposed against the new map.
4. **Nudge staleness.** Features in `discovery`/`delivering` with no linked activity for a config
   window get a low-key indicator on the board. No sessions, no alarms.

**Rejection is signal.** A rejected proposal moves the item to `needs_human` (no blind retry);
past human corrections are surfaced in-context to future classifier prompts as ground truth.
Every proposal/accept/reject emits to the automation event stream.

## 7. API

New router `server/api/pm.py` (pattern: `server/api/sdlc.py`), mounted under `/api/pm`:

- `POST /projects`, `GET /projects`, `GET /projects/{slug}` — registry + wizard state.
- `POST /projects/{slug}/sync` — sync-now. `POST /projects/{slug}/propose-map`,
  `POST /projects/{slug}/classify` — wizard actions (idempotent; 409 if already in flight).
- `GET /triage` — inbox (pending proposals, filterable). `POST /triage/{id}/accept` (optional
  edited values), `POST /triage/{id}/reject`, `POST /triage/accept-bulk`.
- `GET /projects/{slug}/features`, `GET /projects/{slug}/backlog` — board data.
- `GET /roadmap` — scored, ordered initiatives + backlog items, filterable by project/feature/stage.
- `PATCH /work-items/{id}/overlay` — manual classification / inline score edits.
- `PATCH /initiatives/{slug}/scores` — inline initiative score edits (extends the sdlc router's
  initiative surface; scores live on the Initiative row).
- Edges/links: `POST|DELETE /projects/{slug}/edges`, `POST|DELETE /features/{id}/links`.

All propose/accept mutations audit-logged like `sdlc.initiative.*` events.

## 8. Surfaces

New **Products** area in the web nav. Existing idioms throughout: React Query + WebSocket
invalidation (the `useInitiativesLive` pattern), metro-track visual language, shadcn primitives.

- **Triage inbox — the daily driver.** One flat queue across all projects (filterable), newest
  first: title/repo/number, proposed feature, scores, rationale, confidence flag. Keyboard-first:
  `j/k` move, `a` accept, `e` edit-then-accept, `r` reject, `x` select, `A` accept-selected.
  Optimistic UI over the proposal→overlay copy. `no_fit` proposals show the suggested new-feature
  name. Header shows queue depth per project.
- **Per-project product board.** Three panes: (1) the feature map — capability tree with lifecycle
  badges, doc links at commit SHA (`ArtifactLink` pattern), staleness indicators, cross-project
  `powered_by` chips navigating to the other board; (2) initiatives in flight — the existing metro
  track filtered to this project, reused not rebuilt; (3) backlog grouped by feature, ordered by
  score, with an "awaiting triage" bucket linking to the inbox.
- **Priority roadmap.** A ranked list, not a Gantt chart: initiatives + scored backlog ordered by
  `reach × impact × confidence / effort`, inputs shown on hover so the number stays arguable,
  filterable by project/feature/stage, inline score editing. No timeline lanes or drag-scheduling
  in v1.
- **Project registry & wizard.** Projects index (cards: name, repos, onboarding/live state, edges)
  fronting the wizard; the wizard route renders different actions per `onboarding_state`. Sync-now
  button lives here.

Portfolio dashboard trails (edges + links are in the DB from phase 1; the rollup is pure
presentation later).

## 9. Testing

- **Pure engines first**: triage engine + onboarding transitions are I/O-free; tests enumerate the
  full `(state, counts, mode)` behavior table, mirroring `test_sdlc_engine.py`.
- **Loop tests with injected seams** (mirroring `test_sdlc_intake_loop.py`): canned GraphQL
  payloads; assert upsert idempotency, cursor resumption, watermark-advances-only-on-success
  including failure-mid-pass.
- **Frontmatter round-trip tests**: parse → serialize → parse stability; malformed-doc tolerance.
- **API tests** (`test_pm_api.py`, style of `test_sdlc_api.py`): one narrated
  register → backfill → propose-map → merge → classify → accept flow, plus edge cases.
- **Schema discipline**: PG-suite list entries + Alembic revision in the same commit as each model;
  PG suite run locally before push.
- **Web**: vitest for the inbox keyboard state machine, hooks, optimistic-accept rollback;
  `tsc --noEmit` + eslint. Full four-step `lint-and-test` gate before every push.

TDD throughout (constitution V).

## 10. Phasing

Each phase independently useful, each its own implementation plan and PR series:

1. **Foundation + sync** — all models/migrations, `pm-sync` (backfill + incremental + feature
   indexing), registry API + minimal UI. *Payoff: real backlogs mirrored and visible in ccswitch.*
2. **Archaeology onboarding** — wizard, synthesize-map session + PR path, classification sessions
   + `TriageProposal`, functional-but-plain proposal review list. *Payoff: onboard ccswitch
   itself — the dogfood moment.*
3. **Continuous loop + real inbox** — `pm-triage` engine/loop, drift detection, keyboard-first
   inbox. *Payoff: the system stays curated without being chased.*
4. **Product surfaces + advanced structure** — product board, roadmap, discovery-stage behavior
   (prompts + engine handling), cross-project initiative behavior (schema groundwork from
   phase 1). *Payoff: the full PM home; portfolio dashboard trails after.*

## 11. Out of scope (v1)

- Portfolio dashboard build (trails phase 4; data model ships in phase 1).
- Webhooks (poll + sync-now instead; revisit if latency ever matters).
- OKR layer (rejected).
- `auto` mode for any PM automation (human gate is constitutional in v1).
- Multi-user roles/permissions (single-operator Tailscale-identity model, as today).
- Non-GitHub or local-directory projects.
- Timeline/Gantt scheduling.
