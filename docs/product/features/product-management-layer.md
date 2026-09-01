---
slug: product-management-layer
title: Product-Management Layer
state: delivering
links:
  code: [server/pm/onboarding.py, server/pm/classification.py, server/pm/featuredoc.py, server/pm/feature_index.py, server/pm/github_sync.py, server/api/pm.py, web/src/projects/OnboardingWizard.tsx, web/src/projects/TriageReviewList.tsx, web/src/projects/ProjectsTab.tsx, web/src/initiatives/InitiativesTab.tsx]
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
item to `needs_human`, never a silent re-classify (`server/api/pm.py` `reject_triage`). Phase 2
ships a flat triage list, not the keyboard-driven inbox (deferred to phase 3). The engine is
pure/propose-only and never mutates onboarding state on its own.

**Notes** — State is `delivering`: Phase 1 (foundation models + pm-sync backlog mirror + project
registry API/UI, #893) and Phase 2 (archaeology onboarding wizard + map-synthesis prompt +
classify/triage, #897, with the remote-mode RPC-serialization fix #899) have landed; Phases 3–4 are
planned (design/plan docs #866, #879) — Phase 3 adds the continuous triage loop (which reuses
`server/pm/classification.py` verbatim per constitution VI) and the keyboard triage inbox with
edit-then-accept and accept-bulk; the `accept_triage` route already accepts an optional `edits` body
so phase 3 needs no route change. This capability doc set is itself produced by the archaeology
onboarding path dogfooding itself. The classification and onboarding engines are pure functions
(no I/O) mirroring `server/sdlc/engine.py`, so the wizard UI is a thin view over
`next_onboarding_action`.

**Evidence** — `server/pm/onboarding.py` (pure onboarding state machine + `next_onboarding_action`);
`server/pm/classification.py` (`build_map_synthesis_prompt` archaeology + drift prompts,
`build_classification_prompt`, RICE-lite parse, `FEATURE_DOCS_DIR = "docs/product/features"`);
`server/pm/featuredoc.py` (parse/serialize the `<slug>.md` frontmatter convention);
`server/pm/feature_index.py` (diff docs against `Feature` rows, orphan/revive planning);
`server/pm/github_sync.py` (GraphQL backlog mirror for issues + PRs); `server/api/pm.py` (project
registry, sync-now, edges, `/onboarding`, `/propose-map`, `/classify`, `/triage` accept/reject);
`web/src/projects/OnboardingWizard.tsx`, `web/src/projects/TriageReviewList.tsx`,
`web/src/projects/ProjectsTab.tsx`, `web/src/initiatives/InitiativesTab.tsx`;
`specs/001-ccswitch-web/spec.md` (§§3–7). PRs: #866 (design spec), #879 (phase 2–4 plans),
#893 (Phase 1), #897 (Phase 2), #899 (remote-mode seam fix).
