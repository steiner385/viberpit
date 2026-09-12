---
slug: product-management-layer
parent: planning-and-automation
title: Product-Management Layer
state: delivering
links:
  code: [server/pm/onboarding.py, server/pm/classification.py, server/pm/engine.py, server/pm/featuredoc.py, server/pm/feature_index.py, server/pm/github_sync.py, server/pm/repo_project.py, server/pm/score.py, server/pm/health.py, server/pm/tree.py, server/pm/outcomes.py, server/pm/triage_accept.py, server/automations/pm_triage.py, server/automations/product_policy.py, server/api/pm.py, web/src/projects/OnboardingWizard.tsx, web/src/features/roadmap/RoadmapPage.tsx, web/src/features/roadmap/RoadmapListColumn.tsx, web/src/features/roadmap/InitiativePage.tsx, web/src/features/roadmap/detail/AutomationPolicyCard.tsx, web/src/features/roadmap/inbox/InboxScopeBody.tsx, web/src/features/roadmap/inbox/ProposalCard.tsx, web/src/features/roadmap/inbox/ProposalDetail.tsx, web/src/features/roadmap/inbox/inboxKeys.ts, web/src/features/roadmap/inbox/triageSelectors.ts]
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
`auto` triage *mode*: `PmTriage.modes` is still `("off", "propose")` only, the automation row
ships `enabled=False`, and a human accept is the gate on a proposal unless its product has opted
in — phase 2's auto-accept (see "Triage auto-accept" below) is a per-product `triage_auto_accept`
*policy* setting that runs inside `propose` mode, not a third loop mode. Not a re-proposing
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

**Product automation policy (phases 1–3).** Each `Project` carries an opt-in `automation_policy`
(JSON, nullable, `server/db/models.py`) that the Backlog Runner — and, as of phase 2, the PM Triage
loop — read per product instead of one global config row governing every onboarded product alike.
The schema — `PRODUCT_POLICY_SCHEMA` in `server/automations/product_policy.py`, the one
`ConfigField` list and the one validated reader (`effective_policy`) every caller shares
(constitution VI) — has nine fields: `dispatch`
(`off | propose | auto`, default `off` — whether the runner works this product's board at all, and
whether a claimed batch is only announced or actually spawned), `queue_min_status` (default `Ready`,
or `Backlog` to reach untriaged items — how far down the board the runner reaches), `max_in_flight`
(default 1, minimum 0 — this product's concurrent runner sessions, enforced per PRODUCT in the
runner's refill loop rather than translated into a per-repo cap, so a product owning several repos
still runs at most this many sessions total; a `proposed` announce-only claim holds the slot exactly
like a spawned one, a `gave_up` or `handed_off` claim does not. `0` means PAUSED — the product takes
no claim while keeping the rest of its policy — never "unlimited"), `weight` (default 1, minimum 0 —
its share when a freed slot is contested under the `weighted` selection strategy), `max_attempts`
(default 2, minimum 1 — attempts per issue before the runner gives up), `give_up_after_bounces`
(default 2, minimum 0 — landed in phase 3, D8: the bounce threshold both the queue's `needs_spec`
skip and the roadmap's `needs_spec` reason compare against through the one predicate
`server/pm/outcomes.py:bounced_out`; `0` means never give up, and editing the issue's title/body
changes its `body_fingerprint` and restarts the count — see "Dispatch outcomes" below),
`default_model` (default
`auto` — the session model; `auto` assesses each job), `respect_dependencies` (default `true`), and,
landed in phase 2, `triage_auto_accept` (`off | high | medium`, default `off` — the PM Triage loop's
own accept gate, at or above this confidence; `medium` includes `high`; see "Triage auto-accept"
below). The minimums are `ConfigField.min`, enforced in `validate_config` (a below-minimum int is a 422
`bad_policy`, not a browser courtesy) and published in the schema JSON so `ConfigInput` renders the
same floor on the number input. `effective_policy`
never raises: a `None` row (never opted in) and an unparseable one both read as the pure defaults,
i.e. `dispatch: off` and `triage_auto_accept: off`.

It is edited from the product's Overview page (`web/src/features/roadmap/detail/AutomationPolicyCard.tsx`),
an **Automation** card that renders every schema field through the same `ConfigInput` component the
Delivery → Automations Config tab uses (`web/src/features/delivery/automation/ConfigInput.tsx`,
moved out of `ConfigPanel.tsx` and exported so the two surfaces share one renderer instead of two),
disabled with an explanatory note until the product reaches `onboardingState == "live"`. A product
that has never opted in shows every field at its schema default with dispatch `off` and a note that
the first change opts it in; the card always PATCHes the *whole* effective object (every schema key,
current value or default) to `PATCH /api/pm/projects/{slug}/policy` (`server/api/pm.py`, validated
against `PRODUCT_POLICY_SCHEMA`, audited `pm.project.policy`) — so changing any one field, including
`dispatch` itself, writes a complete, non-null policy row, and the product is opted in from that
write on. `GET /api/pm/policy-schema` serves the schema itself to the card.

At the flip, the Backlog Runner's global `enabled_repos` / `repo_weights` / `repo_caps` /
`queue_min_status` / `max_attempts` / `default_model` / `respect_dependencies` knobs retired
(`server/automations/backlog_runner.py`'s `retired_config_keys`); a one-shot seed
(`server/sessions/orchestrator.py`'s `_backlog_seed_policies`, run once per daemon process on the
runner loop's first tick, gated on the runner row being ENABLED and carrying a stored mode — a
never-configured or currently-disabled row has nothing to migrate) carries the old settings onto
the products that owned them so the flip changed no behaviour: the prior global mode becomes each
seeded product's `dispatch` (`auto` stayed `auto`, anything else became `propose`), a product's
primary repo's entry in the old `repo_caps` / `repo_weights` becomes its `max_in_flight` / `weight`
— or, for a product with no repo flagged primary, its lowest-named repo's entry — and the remaining
globals are copied onto every seeded product, but only the keys the legacy config actually set: a
key it left unset is left at the schema default rather than pinned to a made-up value. A non-empty
`enabled_repos` seeded exactly the products owning those repos; the empty list — which meant "every
onboarded product" — seeded every product with a linked board. The seed CONSUMES its own input: in
the same transaction it strips every retired key off the runner row's config and normalises a stored
`propose`/`auto` mode to the phase-1 `on`, and a row carrying none of those keys is what makes the
next call a no-op. That marker is what makes it safe to run on every process start rather than
exactly once in history — durably, unlike the earlier "does any product have a policy" check, which
re-armed the moment an operator cleared the last policy. A runner-card config save strips those keys
too (the config PATCH validates against the phase-1 schema); a row left that way is never seeded
from — its repo list is gone — but its legacy mode is still folded to `on`. A legacy value below a
field's phase-1 minimum (`max_attempts: 0`, a negative per-repo cap) costs that one field its legacy
value, never the whole policy. A product that already carries one is
skipped individually, so a human-set policy is never overwritten and never blocks the rest of the
migration.

What stays global on the runner: whether it runs at all (`off | on` — one row-level toggle;
propose-vs-auto is now the per-product `dispatch` value, not a runner mode), `selection_strategy`
(how a freed slot chooses among the ON products' repos — balanced / priority / weighted /
concurrency-cap), `max_concurrent` (sessions in flight across every product combined),
`max_spawns_per_day`, `max_issues_per_pr`, `grouping_interval_minutes`, and
`needs_human_retry_days` (the runner's own give-up cooldown — a property of the runner's retry
behaviour, not of any one product's board).

Phase 2 of the design has landed: confidence-tiered triage auto-accept per product
(`triage_auto_accept`, §8 — the "Triage auto-accept" paragraph below). **Phase 3 has landed**: the
`dispatch_outcomes` table feeding the bounce-based `needs_spec` skip and roadmap tier reason
(`give_up_after_bounces`, §6–7 — the "Dispatch outcomes" paragraph below). Phase 4 has not: a
history-informed floor on the runner's model pick (§7) — see
`docs/superpowers/specs/2026-09-12-product-automation-policy-design.md` §11 for the phasing.

**Dispatch outcomes (phase 3, spec §6/§7, D5/D8).** Every Backlog Runner dispatch leaves one durable
`dispatch_outcomes` row (`DispatchOutcome`/`DISPATCH_OUTCOMES` in `server/db/models.py`), never
overwritten in place: `id, created_at, updated_at, automation_id, project_id, target_ref,
body_fingerprint, session_id, model, attempt_no, started_at, ended_at, duration_s, outcome, cause,
pr_number, pr_url, first_pass_merge`, every column but `id`/`created_at` nullable so `init_db`'s
reconcile creates the table on both dialects with no hand-written migration default.
`outcome` is one of an eight-value vocabulary, `DISPATCH_OUTCOMES`: `running` (inserted at spawn),
`merged`, `no_pr`, `pr_gave_up`, `needs_human`, `reverted` (reserved, never written in v1),
`abandoned`, and — a phase-3 amendment to the original design's seven-value list —
`superseded`, the row a later dispatch on the same target replaced before it could finish; a
`superseded` row is not counted as a bounce (see "the bounced rule" below).

The two writers are `AutomationContext.record_outcome` / `.finalize_outcome`
(`server/automations/context.py`), both best-effort (spec §13): a DB failure is logged and
swallowed, never blocking a spawn or a sweep. `record_outcome` inserts the `running` row right after
a spawn succeeds — from the loop's own `_refill` (`server/automations/backlog_runner.py`, guarded by
its `_record_outcome` wrapper) and from the two manual taps,
`SessionOrchestrator.backlog_runner_implement_core` (the propose tap) and `.backlog_runner_dispatch`
(manual dispatch) — and it first closes any row still `running` for that target as `superseded`
(`cause=None`; the outcome column alone carries the fact), so at most one `running` row exists per
target even when a tap races the loop's own claim.

`finalize_outcome` closes the row at whichever branch of `BacklogRunnerLoop._sweep_one` ends the
dispatch: a merged PR with a clean close-out → `merged` (with `first_pass_merge`); a merged PR that
still needs a human close-out → `needs_human`; PR Doctor giving up → `pr_gave_up`; the PR closing
unmerged → `abandoned`; the session ending with no mergeable PR and attempts still left →
`no_pr` (a bounce); reaching the attempt cap with the session having never run a turn → `abandoned`;
reaching the attempt cap with a real attempt behind it → `needs_human`. The two attempt-cap branches
pass `upgrade_from=("no_pr",)`: the cap is discovered a sweep AFTER the bounce that carried the
dispatch to it, with nothing re-dispatched in between, so rather than leaving a stray second row for
one session, `finalize_outcome` re-finalises the newest matching row in place (`ended_at`/
`duration_s` kept, outcome/cause replaced) — one dispatch, one row.

`first_pass_merge` is `True`/`False` only when PR Doctor's recovery count for that PR is knowable at
the moment the merge is swept, and `None` otherwise — never guessed. The seam is
`SessionOrchestrator._backlog_pr_doctor_attempts_for`: an unknown target (no PR Doctor state row at
all) answers `None`; a tracked one answers `total_attempts + total_mechanical` (PR Doctor's
monotonic, re-arm-immune counters) plus `lifetime_attempts + lifetime_mechanical` — the same counts
carried forward by `SessionOrchestrator._pr_doctor_lifetime_carry` across PR Doctor's own CI-recovery
clear (`_on_pr_change`, triggered when checks go back to passing). That clear still resets
`total_attempts`/`total_mechanical` to zero, because the give-up ceiling in
`server/automations/pr_doctor_engine.py` reads exactly those keys and a recovered PR earns a fresh
budget — but without the `lifetime_*` carry (which the engine never reads, and which `apply_rearm`/
`record_attempt` preserve through every later cycle because both copy the whole state dict) a
doctor-rescued PR's history would be erased right before the runner's sweep asks whether the merge
was a first pass, and every doctor-rescued merge would misreport `True`.

**The bounced rule (D8).** `server/pm/outcomes.py:body_fingerprint(title, body)` is the one "same
issue body" identity, computed both when a dispatch's `running` row is written (from the mirror
row's title/body at that moment) and again whenever the bounce count is read — so editing an issue's
title or body changes its fingerprint and silently restarts the count; nothing marks it explicit.
`bounces_stmt`/`rows_to_bounces` count only FINALISED, non-`running`/`merged`/`superseded` outcomes
whose `body_fingerprint` matches the issue's CURRENT fingerprint. `bounced_out(bounces,
give_up_after_bounces)` is the one predicate compared against the product's own
`give_up_after_bounces` policy field (`0` = never): the queue's `needs_spec` skip
(`server/pm/queue.py:skip_reason`) and the roadmap's `needs_spec` reason
(`server/pm/tree.py:issue_reason`, folded into `issue_tier`'s `needs-you` short-circuit) both call it
and nothing else duplicates the comparison.

The roadmap surfaces a bounced-out issue as a leaf chip: `web/src/features/roadmap/detail/LeafList.tsx`
renders a `Badge tone="warning"` (`data-testid="leaf-reason"`) whenever a leaf carries a `reason`,
reading `bouncedLabel(leaf.bounces)` — `` `bounced ${n}× — needs spec` `` — for `reason ===
'needs_spec'`, else the generic `reasonLabel(leaf.reason)` for any other reason (`advance`,
`blocked`, `epic_blocked`). `leaf.reason`/`leaf.bounces` come off `server/api/pm_tree.py`'s `_leaf`,
computed once per open item from one `bounces_stmt` execution over the whole page (`_issue_facts`)
rather than a per-leaf query.

Every dispatch an issue has had is readable through `GET /api/pm/work-items/{item_id}/outcomes`
(`server/api/pm.py:get_work_item_outcomes`): 404 `{"error": "not_found"}` for an unknown work item,
else `{"items": [...]}`, newest first (`list_outcomes_stmt`), each row shaped by
`outcome_to_dict` (camelCase: `targetRef`, `prNumber`, `firstPassMerge`, `attemptNo`, …). The web's
`fetchOutcomes`/`useOutcomes` (`web/src/api/pm.ts`, `web/src/api/hooks.ts`) wrap it, unpolled
(dispatch history has no live-update need).

**Triage auto-accept (phase 2).** For a product whose policy sets `triage_auto_accept` above `off`,
`PmTriageLoop._auto_accept_pass` (`server/automations/pm_triage.py`) runs on every `propose`-mode
tick, after the tick's one dispatched engine action (classify/drift/stale/needs-human/idle) —
unconditionally, so a classify-saturated backlog never stalls accepts. It reads every product's
policy through the injected `read_policies` seam (`_pm_triage_load_policies` in
`server/sessions/orchestrator.py`, the same `load_policies` reader phase 1 uses) and lists every
pending proposal as an `AutoAcceptCandidateView` through the `list_auto_accept_candidates` seam
(`_pm_triage_list_auto_accept_candidates`: owning product via `owning_projects_by_repo`, the target
capability's lifecycle `state`, and `auto_undone`). Each candidate is run through the ONE pure gate,
`auto_accept_eligible` in `server/pm/triage_accept.py`: the product's tier and the proposal's
`confidence_level` are ordered by index into `CONFIDENCE_LEVELS` (`server/pm/classification.py`,
ascending `low < medium < high`), so a `medium` policy accepts `medium` and `high` alike; a
`confidence_level` outside that vocabulary — including `None` — reads as `low` and is never
auto-accepted; a `no_fit` proposal (`proposed_feature_id is None`) is never eligible, since creating
a capability stays a human call; neither is a proposal whose target capability is still
`state == "proposed"` (its doc is unmerged) nor one a human has already undone once
(`auto_undone`). An eligible candidate is accepted through the injected `auto_accept` seam
(`_pm_triage_auto_accept`), which re-reads the proposal in its own transaction — a human may have
resolved it since the list, so a `None` return means "nothing left to accept," not a failure — and
calls the SAME `apply_accept` (`server/pm/triage_accept.py`) the inbox routes use, with
`decided_by=DECIDED_BY_AUTO` (the `"auto"` constant, stored on `WorkItem.classified_by`) and
`audit_identity="pm-triage"`, recording the audit type `pm.triage.auto_accept`. A product is capped
at `MAX_AUTO_ACCEPTS_PER_TICK = 25` accepts per tick — a constant in `pm_triage.py`, deliberately
not a `ConfigField`: a blast-radius bound on an unattended writer, not an operator preference. The
pass is fail-safe by construction: either of the pass's two DB reads failing — the policy read or
the candidate listing — skips the whole pass for that tick and emits an `auto_accept_skipped` feed
event, leaving the rest of the tick (including the status-card counts) to finish; one candidate's
accept failure is logged and emits `auto_accept_failed` without aborting the rest of the pass; each
accepted candidate emits `auto_accepted`; and a product that hits its cap emits one
`auto_accept_capped` event, after the loop, naming the product (not the individual proposal ids)
the cap deferred to the next tick. Candidates are read oldest-first and tie-broken on `id`
(`order_by(created_at, id)`), so which of a backlog the cap lets through is the queue order the
inbox shows and not the storage engine's — `created_at` alone is not a total order, since one
classify session writes its whole batch under a single transaction timestamp.

An auto-accept is undoable for the same `TRIAGE_UNDO_WINDOW_S` (60 s) window as a human accept,
through the same `POST /api/pm/triage/{id}/undo` (`undo_triage`, `server/api/pm.py`) — but undoing
one the loop made also sets `TriageProposal.auto_undone = True` so the gate above refuses to ever
re-accept that proposal on a later tick; undoing a human accept the same way leaves `auto_undone`
unset. Who decided is read from the proposal's OWN record: `apply_accept` stamps
`undo_snapshot["decidedBy"]` (beside the pre-decision overlay, not inside it — `_restore_overlay`
reads named overlay keys only and ignores it), and `undo_triage` keys `auto_undone` off that. It
must not read `WorkItem.classified_by`, which is live overlay state any overlay PATCH rewrites to
`"human"` — score an auto-accepted item inside its undo window, which is exactly what the chip
invites, and the machine's authorship is gone, so the flag stays NULL and the loop re-accepts the
proposal on its next tick. `list_triage`'s query widens to match the chip: alongside every `pending`
proposal it now also returns proposals that are `accepted`, resolved within the undo window, AND
whose work item carries `classified_by == DECIDED_BY_AUTO` — so a fresh auto-accept stays visible in
the Inbox for exactly as long as it is undoable, then drops out of the list like a human accept's
undo toast expires. That widening is for ONE surface, so `?pendingOnly=true` suppresses it and
answers exactly `status == "pending"`; the assistant's `list_triage` tool passes it, because a
decided row is not "awaiting a human decision". On the web the same distinction is one exported
selector, `inbox/triageSelectors.ts` (`isPending` / `pendingProposals`), which the nav badge, the
Inbox scope count, the bulk-selection prune and the row itself all read — no call site compares
`status` for itself. `_triage_to_dict` carries `classifiedBy` and `resolvedAt` on every row so the
web can tell a fresh auto-accept apart from a pending one. `ProposalCard.tsx` renders an
`auto-accepted` info
badge plus an inline Undo button (calling the ONE `useUndoProposal` hook — no second undo path) on
exactly those rows, and disables rather than hides the row's bulk-select tick, so the leading rail
stays aligned across a list mixing pending and fresh-auto-accepted rows. `InboxScopeBody.tsx`'s
keyboard handlers (`a`/`r`/`e`/`x`) and its Select-All bulk-accept both no-op on a cursor or
selection landing on a non-`pending` row, so a keystroke can never re-decide what the loop already
accepted.

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
with the latch above); `server/pm/triage_accept.py` (the ONE `apply_accept`, shared by the inbox
routes and the loop's auto-accept, and the pure `auto_accept_eligible` gate);
`server/automations/pm_triage.py` (`PmTriage` + `PmTriageLoop`: the
mechanical auto-link pass, one engine action per tick, rolling daily classify-session cap, the
`_auto_accept_pass`); `server/sessions/orchestrator.py` (`_pm_triage_*` seams, including
`_pm_triage_load_policies` / `_pm_triage_list_auto_accept_candidates` / `_pm_triage_auto_accept`,
and the loop's primary-only task);
`server/automations/pm_onboard.py`, `server/automations/pm_classify.py` (the two session
origins' audited engine ceilings and their operator-triggered `modes = ()` declaration);
`server/pm/repo_project.py` (the one repo→project resolver); `server/pm/score.py`
(`rice_score` + `validate_rice` — the RICE formula and its input bounds in one place);
`server/api/pm.py` (project registry, sync-now, edges, `/onboarding`, `/propose-map`, `/classify`,
`/triage` accept/reject/accept-bulk/undo, `/work-items/{id}/overlay`);
`web/src/features/roadmap/RoadmapPage.tsx` (the portfolio / product / capability detail levels,
with `OnboardingWizard` and `DependencyPanel` mounted as product tabs),
`web/src/features/roadmap/RoadmapListColumn.tsx` (the Map, Initiatives and Inbox list scopes),
`web/src/features/roadmap/InitiativePage.tsx`, `web/src/projects/OnboardingWizard.tsx`,
`web/src/features/roadmap/inbox/InboxScopeBody.tsx` + `inboxKeys.ts` + `triageSelectors.ts` +
`ProposalCard.tsx` + `ProposalDetail.tsx` (the keyboard-first inbox: `j/k/a/e/r/x/A`, optimistic accept with a
six-second server-backed Undo, cursor anchored to a proposal id so a queue that reorders under
the user cannot misdirect a keystroke, and the "auto-accepted" chip + Undo for the loop's own
accepts);
`specs/001-ccswitch-web/spec.md` (§§3–7). PRs: #866 (design spec), #879 (phase 2–4 plans),
#893 (Phase 1), #897 (Phase 2), #899 (remote-mode seam fix), #1327 (Phase 3),
#1268/#1269 (Codex parity for the onboarding, critic and classification sessions).
