---
slug: product-management-layer
title: Product-Management Layer
state: delivering
links:
  code: [server/pm/onboarding.py, server/pm/classification.py, server/pm/engine.py, server/pm/featuredoc.py, server/pm/feature_index.py, server/pm/github_sync.py, server/pm/repo_project.py, server/pm/score.py, server/automations/pm_triage.py, server/api/pm.py, web/src/projects/OnboardingWizard.tsx, web/src/projects/ProjectsTab.tsx, web/src/triage/TriageInbox.tsx, web/src/initiatives/InitiativesTab.tsx]
  prs: ['#866', '#879', '#893', '#897', '#899']
---

# Product-Management Layer

**Problem** — ccswitch's autonomous SDLC can *build* work, but it had no product-management
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
continuous `pm-triage` loop + the keyboard-first Triage Inbox) have landed. Phase 3 reuses
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
`server/pm/repo_project.py` (the one repo→project resolver); `server/pm/score.py`
(`rice_score` + `validate_rice` — the RICE formula and its input bounds in one place);
`server/api/pm.py` (project registry, sync-now, edges, `/onboarding`, `/propose-map`, `/classify`,
`/triage` accept/reject/accept-bulk, `/work-items/{id}/overlay`);
`web/src/projects/OnboardingWizard.tsx`, `web/src/projects/ProjectsTab.tsx`,
`web/src/triage/TriageInbox.tsx` + `keyboardState.ts` (the keyboard-first inbox: `j/k/a/e/r/x/A`,
optimistic accept, cursor anchored to a proposal id so a queue that reorders under the user cannot
misdirect a keystroke), `web/src/initiatives/InitiativesTab.tsx`;
`specs/001-ccswitch-web/spec.md` (§§3–7). PRs: #866 (design spec), #879 (phase 2–4 plans),
#893 (Phase 1), #897 (Phase 2), #899 (remote-mode seam fix).
