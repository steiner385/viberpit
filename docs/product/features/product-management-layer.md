---
slug: product-management-layer
parent: planning-and-automation
title: Product-Management Layer
state: delivering
links:
  code: [server/pm/onboarding.py, server/pm/classification.py, server/pm/engine.py, server/pm/featuredoc.py, server/pm/feature_index.py, server/pm/github_sync.py, server/pm/repo_project.py, server/pm/score.py, server/pm/health.py, server/pm/tree.py, server/automations/pm_triage.py, server/api/pm.py, web/src/projects/OnboardingWizard.tsx, web/src/features/roadmap/RoadmapPage.tsx, web/src/features/roadmap/RoadmapListColumn.tsx, web/src/features/roadmap/InitiativePage.tsx, web/src/features/roadmap/inbox/InboxScopeBody.tsx, web/src/features/roadmap/inbox/ProposalDetail.tsx, web/src/features/roadmap/inbox/inboxKeys.ts]
  prs: ['#866', '#879', '#893', '#897', '#899', '#1327']
---

# Product-Management Layer

Maintain a durable capability map and connect prioritized work to the product outcomes it changes.

**Problem** — ViberPit's autonomous SDLC can *build* work, but it had no product-management
brain to decide *what the work is*. There was no durable model of what a product actually is
(its capabilities), no place to record outcome-driven feature intent, and no principled way to
route an open backlog into capability areas. For an existing project with hundreds of open and
closed issues/PRs, the "feature map" lived only in maintainers' heads — invisible, un-curated,
and impossible for an autonomous loop to reason over. Onboarding a real codebase into that model
by hand would be prohibitively slow.

**Target users** — The project owner / maintainer curating the product (they merge the proposed
feature map and drain the triage queue), and the autonomous SDLC loop itself, which consumes the
resulting feature registry and classified backlog as its source of "what to work on."

**Desired outcome** — A durable, machine-readable *feature map* reconstructed from an existing
codebase + backlog with minimal human effort. Each capability is captured as an outcome-driven
feature doc (`docs/product/features/<slug>.md`: Problem / Target users / Desired outcome / Success
metrics / Non-goals / Notes / Evidence) under YAML frontmatter that indexes into `Feature` rows.
An onboarding wizard walks a newly-registered project through a fixed state machine
(`registered → syncing → map_proposed → map_merged → classifying → live`), backfilling the backlog
mirror, proposing the map via a single non-draft PR, then classifying open items into capabilities
with RICE-lite scores and a `no_fit` escape hatch. Every proposal is human-gated: the map PR merge
and the triage inbox are the curation gates; the engine never advances state itself.

**Success metrics** — (unknown — needs owner input). Candidate measures the code makes possible to
track: share of open backlog items classified vs. `no_fit`/`needs_human`; triage-proposal
acceptance rate vs. rejection rate; number of curated feature docs indexed per project; time from
project registration to `live` state.

**Non-goals** — Not an auto-merging step: the map-synthesis session opens exactly one non-draft PR
and deliberately does NOT arm auto-merge — the human merge is the curation gate
(`server/pm/classification.py`). Not a blind-retry classifier — a rejected proposal sends the work
item to `needs_human`, never a silent re-classify (`server/api/pm.py` `reject_triage`). Not an
`auto` mode: `PmTriage.modes` is `("off", "propose")` only, the automation row ships
`enabled=False`, and a human accept is the gate on everything it proposes. Not a re-proposing
loop — the drift branch is latched (see Notes), so one unnamed capability yields one map-diff PR,
not one per tick. The engine is pure/propose-only and never mutates onboarding state on its own.

**Notes** — State is `delivering`. Phase 1 (foundation models + pm-sync backlog mirror + project
registry API/UI, #893), Phase 2 (archaeology onboarding wizard + map-synthesis prompt +
classify/triage, #897, with the remote-mode RPC-serialization fix #899) and **Phase 3** (the
continuous `pm-triage` loop + the keyboard-first triage inbox, now the Inbox scope of the Roadmap
section, `/roadmap?scope=inbox`) have landed. Phase 3 reuses
`server/pm/classification.py` and the phase-2 `pm_classify_run` / `pm_onboard_spawn_map`
orchestrator seams rather than adding a second classifier or map-synthesis path (constitution VI),
and needed no schema change — every concern routes through phase-1's `0012_pm_product_layer`.

Two Phase-3 design points worth knowing before changing this code:

- **The drift branch is latched.** `no_fit` clusters are *permanent* — accepting a proposal keeps
  its `no_fit_suggestion` as audit — so a cluster stays over threshold forever. Without a latch
  every tick would re-propose the same capability, and the spawn-time dedupe cannot substitute:
  `AutomationContext.spawn_session` matches only *non-terminal* sessions, so each time the map-diff
  session finishes the next tick would open another PR. `TriageState.open_map_diff_suggestions`
  (populated from an existing `Feature` slug **or** a live map-diff session) excludes them, the way
  `server/sdlc/engine.py` returns `AwaitApproval` while a stage's artifact PR is open.
- **`Feature.slug` is unique per `(project_id, slug)`, not globally** (`uq_feature_slug`). Every
  lookup by bare slug must be project-scoped, via `server/pm/repo_project.py` — the one resolver
  for "which project owns this repo", shared by the API and the orchestrator. Unscoped, a drift
  accept binds a work item to another *product's* feature.

**The surface is one Roadmap section, not a Products tab and an Initiatives tab.** As of the
roadmap drill-down spec (`docs/superpowers/specs/2026-09-07-roadmap-drilldown-design.md` §4, its
own phase 3 — numbering independent of this doc's PM-layer phases; finish-line spec
`2026-09-08-finish-line-persona-modes-design.md` §4), everything this capability renders lives
under the nav section `roadmap`, on the same master/detail chassis as Sessions:

- `/roadmap` — the portfolio. The list column carries two scopes, **Map** (the product →
  capability tree, from `GET /api/pm/map`) and **Initiatives** (every initiative, grouped by
  stage); the detail pane is the portfolio's Needs you / Ranked / All initiatives tabs.
- `/roadmap/:productSlug` — one product: overview, in flight, needs you, ranked, dependencies.
  This is where the onboarding wizard and the dependency panel are mounted.
- `/roadmap/:productSlug/capabilities/:capSlug` — one capability, with its sub-capabilities,
  in-flight initiatives and issues.
- `/initiatives/:initiativeSlug` — one initiative (track, epic, sessions, capabilities, and the
  Advance human gate). It keeps its own prefix and is claimed for the Roadmap section by a nav
  alias, so the list column stays beside it.
- Tabs are URL state (`?tab=`), so every level is linkable.
- `/roadmap?scope=inbox[&product=<slug>][&proposal=<id>]` — the **Inbox** scope: the triage queue,
  grouped by product, with the proposal editor as its detail pane. Accept / reject / bulk-accept
  are optimistic and each raises a six-second Undo toast backed by `POST
  /api/pm/triage/{id}/undo`.
- `/projects` → `/roadmap`, `/projects/:slug` → `/roadmap/:slug`, `/initiatives` →
  `/roadmap?scope=initiatives` and `/triage[?projectSlug=x]` →
  `/roadmap?scope=inbox[&product=x]` redirect; `ProjectsTab`, `InitiativesTab`, `TriageInbox` and
  their css are deleted. The queue's depth badges the **Roadmap** nav entry, which is what points
  at it from outside the section now that Triage is not a section.

Still owed by that spec: issue leaves and inline overlay edits (phase 4), the Queue scope and the
Inbox's "Needs a home" Reclassify control (phase 6).

**Roadmap hygiene (5S plan, 2026-09-12).** A feature doc's `labels:` frontmatter list mirrors onto
`Feature.route_labels`; pm-triage's mechanical pass (`_pm_triage_route_by_label`, before the AI
classifier ever sees the item) case-folds a labelled item's GitHub labels and, when exactly one
feature in the item's owning project claims one of them, links it straight in
(`classified_by="label-route"`) — so a bot-filed issue carrying a recognized label (`flaky`,
`forced-deploy-audit`, …) never clusters into a proposed capability the classifier invented. The
lookup is served from a per-repo route-map cache rebuilt at most once every 60s, not a DB session
per labelled item per tick. `_pm_sync_apply_feature_plan` also now advances a project sitting at
`registered` or `syncing` straight to `map_merged` once it has indexed ≥1 feature doc off the
*default* branch (`commit_sha` set — a still-open map-changes PR does not count), closing the gap
where a hand-merged map left onboarding permanently claiming "not onboarded". Initiatives nest the
same way GitHub epics do: `Initiative.parent_initiative_id` mirrors a sub-issue epic's parent onto
the adopted initiative, so a **program** (an initiative with phases) reports a derived stage (its
earliest non-done phase) and tier from its **phases**, rolls up leaf-issue progress across every
phase rather than counting its own epic's direct sub-issues (which would be sub-epics, not leaves),
and is never advanced, stepped back, or GitHub-Stage-back-synced directly — only a leaf initiative
is. `GET /api/pm/projects/{slug}/health` (`server/pm/health.py`) is the roadmap-drift readout: loose
capabilities outside the tree, initiatives linked to no capability (a program is judged by its
phases, and a `done` initiative is finished rather than drifting, so neither counts), stale
`proposed` leaves, and the inbox's oldest untouched proposal.

**Map-changes PRs (spec §9) — backend and UI landed (row 8c).** A capability edit made through
`POST /api/pm/projects/{slug}/map-changes` (`create` | `rename` | `reparent` | `summary` | `links`)
is committed by the server itself — no agent session — onto one long-lived ViberPit-owned branch
per product, `pm/map-changes-<slug>`, through GitHub's `createCommitOnBranch` (atomic, so the
shared clone is never mutated); the first edit opens the PR, `DELETE` abandons it (close + delete
the branch + drop the capabilities that only ever lived on it) and `POST …/map-changes/rebase`
runs GitHub's own update-branch while the PR is behind. The sync loop indexes the branch head as
well as the default branch: a doc that is **new on the branch or changed there** overrides the
default-branch doc of the same slug and shows as **proposed** until the PR merges, while a doc the
branch merely inherited unchanged is left alone.

The roadmap UI for all of this is row 8c: a capability's Overview gets **Edit** (title, summary,
parent, sent as up to three ops) and **+ Sub-capability**; a product's Overview gets **+
Capability**. A capability's Overview also gets the links chip editor (`CapabilityLinksEditor`, one
`links` op per change); links belong to a capability, so the product's Overview has **+ Capability**
and the PR card only. That card is `MapChangesCard` — the open PR's number/link and age, Rebase
when `behind`, a danger banner when `conflicting`, and Abandon behind a typed-slug confirm — above
the onboarding wizard. A capability row in the Map scope's tree badges its own `prNumber` (`PR #N`,
`TreeRowCard`), and its Overview carries the one-line "proposed in PR #N" note until the PR merges.

The pointer row (`feature_map_changes`) carries more than §9 spells out, all written by the
per-pass PR read: `head_sha` (the branch's current head), `behind` and `conflicting` (from
GitHub's `mergeable_state`: `behind` offers Rebase, `dirty` raises the conflict banner) and
`last_error` (why the last PR read failed — recorded WITHOUT changing the status, so a transient
502 never abandons a live PR). On a `Feature` row the provenance columns are exclusive:
`commit_sha` is the default-branch SHA the doc was read at and is **NULL on a proposed row**,
which instead carries `pr_number`/`pr_url` — and that exact pair (`pr_number = N AND commit_sha IS
NULL`) is what abandon cleanup deletes on.

**Phase 4 status:** superseded rather than pending. RICE→Score and the global queue landed in
#1085, the portfolio UI is tracked as #993, and spec 003 (`specs/003-gh-projects-integration`)
states it supersedes the freestanding Phase 4 model. The one piece 003 did not absorb is the
optional **discovery** SDLC stage, which is **deferred, not planned**: it would prepend a stage to
the live `STAGE_SEQUENCE`, affecting every existing initiative, and it currently has no consumer.
Revive it only when a concrete initiative needs a discovery artifact gate ahead of `intake`; the
design for it is preserved in `docs/superpowers/plans/2026-08-31-pm-product-layer-phase4.md` Task 2.

This capability doc set is itself produced by the archaeology onboarding path dogfooding itself.
The classification, onboarding and triage engines are pure functions (no I/O) mirroring
`server/sdlc/engine.py`, so the wizard UI is a thin view over `next_onboarding_action` and the
triage loop is a thin executor over `next_triage_action`.

The three sessions this capability spawns — map synthesis, its completeness critic, and
classification — are provider-neutral as of #1268/#1269. `pm-onboard` and `pm-classify` are
registered automation origins with a `codex_allowed` engine ceiling, so an operator may run them on
Codex from the same per-origin `engine` config every other origin uses; `codex_allowed` grants the
option, not the traffic, and the default stays Claude behind two further kill switches. They are
*operator-triggered* origins (`modes = ()`): registered for their audited ceiling and their config
row, not because anything ticks them, so the Automations card shows their config and no enable
toggle. Each spawn site resolves the engine ONCE, ahead of the account probe and the clone, and
threads it into the seed prompt's commit-attribution trailer, the engine-scoped account gate and the
session — a refused launch costs nothing and advances no onboarding state. Live evidence for the
bounded classification batch and for synthesis/critic review separation on the real `codex` binary:
`server/tests/test_pm_codex_live.py` (opt-in, `CCSWITCH_TEST_CODEX_LIVE=1`).

**Evidence** — `server/pm/onboarding.py` (pure onboarding state machine + `next_onboarding_action`);
`server/pm/classification.py` (`build_map_synthesis_prompt` archaeology + drift prompts,
`build_classification_prompt`, RICE-lite parse, `FEATURE_DOCS_DIR = "docs/product/features"`);
`server/pm/featuredoc.py` (parse/serialize the `<slug>.md` frontmatter convention);
`server/pm/feature_index.py` (diff docs against `Feature` rows, orphan/revive planning);
`server/pm/github_sync.py` (GraphQL backlog mirror for issues + PRs);
`server/pm/engine.py` (pure `next_triage_action` — the classify → drift → stale priority ladder,
with the latch above); `server/automations/pm_triage.py` (`PmTriage` + `PmTriageLoop`: the
mechanical auto-link pass, one engine action per tick, rolling daily classify-session cap);
`server/sessions/orchestrator.py` (`_pm_triage_*` seams + the loop's primary-only task);
`server/automations/pm_onboard.py`, `server/automations/pm_classify.py` (the two session
origins' audited engine ceilings and their operator-triggered `modes = ()` declaration);
`server/pm/repo_project.py` (the one repo→project resolver); `server/pm/score.py`
(`rice_score` + `validate_rice` — the RICE formula and its input bounds in one place);
`server/api/pm.py` (project registry, sync-now, edges, `/onboarding`, `/propose-map`, `/classify`,
`/triage` accept/reject/accept-bulk, `/work-items/{id}/overlay`);
`web/src/features/roadmap/RoadmapPage.tsx` (the portfolio / product / capability detail levels,
with `OnboardingWizard` and `DependencyPanel` mounted as product tabs),
`web/src/features/roadmap/RoadmapListColumn.tsx` (the Map, Initiatives and Inbox list scopes),
`web/src/features/roadmap/InitiativePage.tsx`, `web/src/projects/OnboardingWizard.tsx`,
`web/src/features/roadmap/inbox/InboxScopeBody.tsx` + `inboxKeys.ts` + `ProposalDetail.tsx` (the
keyboard-first inbox: `j/k/a/e/r/x/A`, optimistic accept with a six-second server-backed Undo,
cursor anchored to a proposal id so a queue that reorders under the user cannot misdirect a
keystroke);
`specs/001-ccswitch-web/spec.md` (§§3–7). PRs: #866 (design spec), #879 (phase 2–4 plans),
#893 (Phase 1), #897 (Phase 2), #899 (remote-mode seam fix), #1327 (Phase 3),
#1268/#1269 (Codex parity for the onboarding, critic and classification sessions).
