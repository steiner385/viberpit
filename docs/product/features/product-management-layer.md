---
slug: product-management-layer
parent: planning-and-automation
title: Product-Management Layer
state: delivering
links:
  code: [server/pm/onboarding.py, server/pm/classification.py, server/pm/engine.py, server/pm/featuredoc.py, server/pm/feature_index.py, server/pm/github_sync.py, server/pm/repo_project.py, server/pm/score.py, server/pm/health.py, server/pm/evidence.py, server/pm/tree.py, server/pm/attention.py, server/pm/hygiene.py, server/pm/dep_prune.py, server/automations/needs_you_hygiene.py, server/pm/closeout_assess.py, server/automations/closeout_assessor.py, server/pm/outcomes.py, server/pm/triage_accept.py, server/automations/pm_triage.py, server/automations/product_policy.py, server/api/pm.py, server/api/pm_hygiene.py, web/src/projects/OnboardingWizard.tsx, web/src/features/roadmap/RoadmapPage.tsx, web/src/features/roadmap/RoadmapListColumn.tsx, web/src/features/roadmap/InitiativePage.tsx, web/src/features/roadmap/detail/AutomationPolicyCard.tsx, web/src/features/roadmap/inbox/InboxScopeBody.tsx, web/src/features/roadmap/inbox/ProposalCard.tsx, web/src/features/roadmap/inbox/ProposalDetail.tsx, web/src/features/roadmap/inbox/inboxKeys.ts, web/src/features/roadmap/inbox/triageSelectors.ts]
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
mirror, proposing the map via a single draft PR, then classifying open items into capabilities
with RICE-lite scores and a `no_fit` escape hatch. Every proposal is human-gated: the map PR merge
and the triage inbox are the curation gates; the engine never advances state itself.

**Success metrics** — (unknown — needs owner input). Candidate measures the code makes possible to
track: share of open backlog items classified vs. `no_fit`/`needs_human`; triage-proposal
acceptance rate vs. rejection rate; number of curated feature docs indexed per project; time from
project registration to `live` state.

**Non-goals** — Not an auto-merging step: the archaeology map-synthesis session opens exactly one
DRAFT PR and deliberately does NOT arm auto-merge or ready it — a human readies and merges it, the
curation gate (`server/pm/classification.py`). A draft because many repos arm auto-merge on every
ready PR themselves, which made the old "non-draft, unarmed" gate unenforceable (#2925). Not a blind-retry classifier — a rejected proposal parks the work
item under an **owner-action hold** (`server/api/pm.py` `reject_triage` → `server/pm/holds.py`
`set_reject_hold`), and it is that hold, not the item's triage state, that keeps it out of the
classify loop's candidate set — never a silent re-classify. Not an
`auto` triage *mode*: `PmTriage.modes` is still `("off", "propose")` only, the automation row
ships `enabled=False`, and a human accept is the gate on a proposal unless its product has opted
in — phase 2's auto-accept (see "Triage auto-accept" below) is a per-product `triage_auto_accept`
*policy* setting that runs inside `propose` mode, not a third loop mode. Not a re-proposing
loop — the drift branch is latched (see Notes), so one unnamed capability yields one map-diff PR,
not one per tick. The engine is pure/propose-only and never mutates onboarding state on its own.

## Holds

THE single description of the hold ladder (spec `docs/superpowers/specs/2026-09-18-hold-ladder-design.md`,
landed items 1–6). A work item may carry one hold: `hold_kind` (`evidence`, `stronger_model`,
`decision`, `owner_action`), `hold_reason` (mandatory), `hold_by` (the actor string: a person's
login, `session:<id>`, `assistant:<login>`, `lan@local`, `runner`, `automation:<id>`, `migration`),
`hold_set_at`, and `hold_fingerprint` (the body fingerprint at hold time). A person may set or clear
any kind; a session, the assistant, or a loopback caller may set or clear only `evidence` and
`stronger_model` (`server/pm/holds.py:AGENT_SETTABLE`), plus `decision` through the one proposal
route below; the runner and the automations write whichever kind their own flow owns but are never
counted as "human" (`server/pm/holds.py:actor_class`). No actor may OVERWRITE a hold it could not
itself clear — replacing a kind releases the old one first. Rejecting a classification proposal
writes an `owner_action` hold by the rejecting actor this way (spec §8 authorises it from "the two
reject flows" whoever drives them, through `holds.py:set_reject_hold`, no actor-class gate — it
records `lan@local` or `assistant:<login>` truthfully rather than attributing the reject to a
person); migration 0049 (`scripts/convert_legacy_needs_human.py`) converted the surviving pre-ladder
rows on the live DB. Those conversion holds (`hold_by = migration`) record no decision, so the
reconcile pass releases each once it is older than `MIGRATION_HOLD_TTL` (7 days), audited with
signal `expired` (#2682) — the one hold time ends. Every overlay write still records its actor in `classified_by`.

Only `owner_action` and a pending `decision` derive the board's "Held" status and the roadmap's
needs-you tier; `evidence` and `stronger_model` are agent-settable AND queue-eligible, so an item
under either one still reads `Ready`/`Backlog` and rides the Backlog Runner's ordinary queue until
it is actually dispatched — a card is never shown "Held" for work nothing is parked on.

**Decision escalation.** The `decision-escalation` automation (default off) watches held items per product and sends one `decision_needed` digest when decisions are starving dispatch, ageing, or blocking a lot of work; it never resolves a decision itself. See `docs/product/features/decision-escalation.md`.

An `owner_action` hold ends only on a human signal (spec §7): a board edit to Ready or Backlog, an
issue body edit (the fingerprint no longer matches), or `POST
/api/pm/work-items/{id}/hold/release` (the Retry tap) — all three through
`server/pm/holds.py:release_by_signals` / `clear_hold`, each audited `pm.hold.release` with its
`signal` from the one `RELEASE_SIGNALS` tuple. An explicit `clearHold` through the overlay editor is
a fourth spelling (`signal: "overlay"`) and, unlike the other three, stamps `classified_by`. A
fifth, `signal: "escalation"`, is `POST /api/pm/work-items/{id}/hold/approve-escalation` — only
for a hold the runner wrote at an approval-clamped ladder top ("bounced at opus; fable needs
approval"; `server/pm/outcomes.py:escalation_for`): it releases the hold AND dispatches the issue
now at the approved tier, resolving the model gate's parked session as the tapping person (see the
Backlog Runner feature doc, ladder section). The
roadmap's Review-and-decide dialog shows the hold (kind, reason, who, when) and offers **Retry**
(the release endpoint) and **Implement now** (manual dispatch, which releases the hold with
`signal = implement` first), each with server-computed consequence copy from
`server/pm/explain.py:hold_guidance`; Retry refuses a `decision` hold (422) — that one resolves only
through its proposal, never a plain release. An `evidence` hold instead shows ONE button-less
consequence line from the same function — the Backlog Runner dispatches and settles it on its own,
so there is nothing here for a person to tap — rendered through the same `ConsequenceList` the
owner-action buttons use, never a second renderer. The leaf row shows the hold kind as a chip. The
queue skips held items as `held`; the Backlog
Runner's give-up writes the hold (`hold_by = runner`) and its `gave_up` row survives only until the
hold is gone.

The model ladder (`stronger_model`, item 4a, spec §4): a bounce climbs the NEXT dispatch one model
tier above whatever the previous attempt ran on, up to the product's `ladder_top`; a bounce AT that
top writes `owner_action` instead — the ordinary give-up to the human. `ladder_top` is itself
clamped to whatever the model-approval policy allows before the climb ever reaches it, so a climb
never parks a session as a driverless approval instead of a dispatch. `give_up_after_bounces`
(default `0`, meaning never) is an optional stricter stop in front of the ladder's own top. See
`docs/product/features/autopilot-backlog-runner.md` § The model ladder for the full rule.

Evidence dispatch (`evidence`, item 4b, spec §5): a new, open, auto-filed error report is parked
under an `evidence` hold the moment it enters the mirror, then dispatched as a read-only
INVESTIGATION, never an implementation. The session must end in exactly one of four exits — close
as noise, consolidate into a sibling, convert to a fix, or escalate what only the owner can supply —
and sibling reports of one defect are grouped into a single investigation by error family
(`server/pm/error_family.py:family_key`) rather than racing N sessions to close each other as
duplicates. An item under `evidence` (like `stronger_model`) is still classified normally — only
`owner_action` and a pending `decision` are excluded from the classify-candidate set. See
`docs/product/features/autopilot-backlog-runner.md` § Evidence dispatch for the full rule.

Decision proposals (`decision`, item 5, spec §6): when a session hits a genuine product question it
proposes two to six options instead of guessing or handing back — `POST
/api/pm/work-items/{id}/decisions` with `options` (`key`, `label`, optional `detail`), a
`recommended_key` among them, and a `rationale`. `server/pm/decisions.py:propose` — the one writer,
called in-process by the `propose_decision` session tool and reached over loopback by Codex
sessions — records the proposal row and the item's `decision` hold in ONE transaction, audited
`pm.decision.propose {workItemId, proposalId, actorClass}` plus `pm.hold.set`; only one pending
proposal per item, a database constraint, not just a check. The session then ends with
`mark_no_pr(resolution="awaiting-decision")`, which the Backlog Runner treats as neither a bounce
nor a hand-off.

`GET /api/pm/decisions` lists every pending proposal — `id`, `ref`, `title`, `projectSlug` (the
SAME key the classification list uses, so the Inbox groups both kinds by owning product through one
ordering spec), `options`, `recommendedKey`, `rationale`, `proposedBy`, `createdAt`, and the
two-column `guidance` (`server/pm/explain.py:decision_guidance`, the same `Consequence` wire shape
the classification card renders). The web tags the rows `proposalKind: 'decision'` at fetch time —
the wire itself carries no discriminator — so `triageSelectors.pendingProposals` stays the ONE
"awaiting a human" predicate and the Roadmap nav badge counts both kinds. In the Inbox they list
beside classification proposals with the same keys, as a single-choice group with the
recommendation preselected; `a` OPENS a decision row rather than accepting it (there is no undo
window to take a chosen option back with); `r` does the same — neither key decides a decision from
the list, only the pane's own Accept/Reject buttons do — and `x`/`e`/`A` are inert. Decisions lead every Inbox bucket by what they block (a "Blocks N" chip, `downstream` from the escalation read model). While the recommended option is selected the primary button reads "Accept recommendation" and accepts it in one tap. A one-line starvation notice heads the list when a product's runner is starving (`/pm/holds`).

**Accept** (`POST /api/pm/decisions/{pid}/accept` with `chosen_key`) records the key, clears the
hold — the only path that may — and comments on the issue after the commit (best-effort, bounded by
`DECISION_COMMENT_TIMEOUT_S`); the next implement prompt quotes the accepted row verbatim
(`decisions.accepted_text_for_refs`). **Reject** replaces the hold with the caller's own
`owner_action` park (reason `decision proposal rejected`, `holds.DECISION_REJECT_REASON`), so Retry
and Implement now are the way back out. Both are gated on `decisions.may_resolve` — a person or
`assistant:<login>` only, 403 for a `session:` actor, `lan@local`, or a system actor, since the
machine must not answer the question it raised — and both re-read the proposal row `FOR UPDATE`, so
a race loses with 409 rather than recording two decisions. Audit identities are ACTOR STRINGS, not
people: `pm.decision.propose` is usually `session:<id>`, while `pm.decision.accept {proposalId,
chosenKey}` and `pm.decision.reject` carry whoever actually resolved it. The Review-and-decide
dialog shows a `decision` hold as "Awaiting your decision" with a link to the Inbox and no buttons
of its own — the proposal card is the only way to resolve it.

**The rename to "Held" (item 6, spec §9) has landed.** The board's machine value is `Held`
(`server/pm/desired_state.py:STATUS_HELD`). A board the one-time rename has not yet reached still
spells that column `Needs human`; every read (`is_held_status`) treats both spellings as one
column, and the one write path (`gh_mutations._render`) resolves to whichever spelling that board's
field cache actually carries, so a mixed fleet mid-rollout keeps working with no double option.
`scripts/rename_board_held.py` renames a board's Status option in place — same id, colour,
description and every card's assignment, never a delete-and-recreate — dry-run first
(`python scripts/rename_board_held.py`), then `--apply`; a board carrying both spellings at once is
a `conflict` the script refuses to touch, and needs a human to move the cards off the legacy column
by hand first. Run it once per board, on the edge host, after a deploy that ships this series. It
needs `VIBERPIT_DB_URL` (the live database) and `VIBERPIT_GH_PROJECT_TOKEN` (the legacy name is
mirrored at import) — the DRY RUN needs the token too, since it reads every board live rather than
the cache. The plan it prints is read at plan time and resent as-is on `--apply`; a board column
added between the read and the apply is
dropped with its cards, so only run it when nobody is editing board columns (the window is seconds
per invocation). The apply path has not yet run against real GitHub, so apply one board first
(`--apply --project <slug>`) and read the printed ids before running it across the fleet.
Expect exactly one `gh_human_edit` observation per already-held card right after its board's rename:
the mirror's last-synced value for that card is still the old spelling, the read-back after the
rename reports the new one for the SAME option id, and `upsert_project_items` compares those two
strings literally — a real, one-time, harmless echo, not a person touching the card. The fleet
rename landed and every board reported `done`, so the read alias (`LEGACY_STATUS_NEEDS_HUMAN`) has
since been dropped — `Held` is the sole spelling everywhere (#2015).

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

- `/roadmap` — the portfolio page: its Needs you / Ranked / All initiatives tabs. The list
  column beside it carries the scopes **Map** (`?scope=map`: the product → capability tree, from
  `GET /api/pm/map`), **Initiatives** (`?scope=initiatives`: every initiative, grouped by stage)
  and **Inbox** (below). Every list scope carries its `?scope=`, the Map included: bare `/roadmap`
  is the portfolio *open in its own right*, so below desktop — where the band holds either the
  list or the page — the section opens on the whole-roadmap queue and the tree is one tap away
  behind "Back to Map" (PRODUCT.md principle 1; 2026-09-30 Roadmap critique). On desktop the two
  render side by side and the distinction is invisible.
- `/roadmap/:productSlug` — one product: overview, in flight, needs you, ranked, dependencies,
  automation. This is where the onboarding wizard and the dependency panel are mounted.
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

**Acting on attention.** Product and capability overviews show **Next actions** with links to
initiative decisions, issue lists, and proposal triage. **Needs you** uses the same selection for
its map badge, summary tile, and tab: initiatives needing input, direct issue groups per
capability, and product triage groups. A capability includes attention under its descendants;
issue groups link to the capability that owns those issues. The selected capability shows its
attention issues inline, with guidance and actions. Counts include individual attention issues
plus initiative and product triage rows. Incidents retain their separate live queue.

**Needs you vs waiting** (design `docs/superpowers/specs/2026-10-14-needs-you-burden-design.md`
§1; the one definition is `server/pm/attention.py`). An item needs you only when a human alone can
unblock it: a `decision` or `owner_action` hold, a session waiting on a person, a bounced-out issue
that needs a better spec, an initiative gate (`awaiting_human`, or `advance`), triage proposals,
owner-held items with no capability, or an open issue assigned on GitHub to the product owner (a
human login in the policy's `trusted_authors`). A program counts for its OWN tier (`ownTier`),
never once more per phase. Everything else that is stopped is **waiting** and is never counted: a
bare board `Held` card (no hold, no human-wait session) tiers `blocked`, not needs-you, and an
epic's `held` roll-up counts only open sub-issues with a real hold. Blocked initiatives and blocked
issue groups list under a quiet **Waiting** section of the Needs-you tab. Each product's
`counts.waiting` (`GET /api/pm/map`) buckets every row the queue skips by reason (`blocked`,
`boardBlocked`, `boardHeld`, `parked`, `epicOpen`, `inProgress`, `tracker`, `notNeeded`,
`assigned`, `offQueue`, `done`, `bot`, `untrusted`, `environment`), from the same `queue_inputs` →
`rank` composition `GET /api/pm/queue` uses (`product_policy.rank_kwargs`), so the map and the
queue cannot disagree. The product row on the Map says them as muted text ("Waiting: 31 blocked by
a dependency · 28 parked"), never as a badge.

**Needs-you hygiene** (design §2; automation `needs-you-hygiene`, primary-only, 15-minute tick,
modes `off | dry_run | on`). Pure rules in `server/pm/hygiene.py` find what a machine can tidy:
`done_but_open` (a board card Done and untouched for 24 h on an open issue → close as completed,
after a one-line comment; never bot noise, trackers or generated/daily reports such as the Renovate
Dependency Dashboard or `traceability-drift`), `board_blocked_no_blockers` (Blocked with no open
dependency and no hold → the policy's `queue_min_status`, through `board_status.apply`),
`stale_holds` (a system actor's `owner_action` hand-off the queue already skips →
released, signal `policy_skip`; `policy_skip.stale_system_holds` is the ONE rule the backlog tick
shares) and `resolved_edges` (prunable stale dependency edges → `dep_prune.prune`, the same core as
`POST /api/pm/projects/{slug}/deps/prune`), plus (design §3) `closeout_assess`: a system-written
close-out hold ("merged PR left the issue open") is assessed by a stateless helper
(`server/automations/closeout_assessor.py`, ≤ 3 per tick, one per issue body) and becomes a close
(every item done, with evidence), a release (signal `assessment`: an agent can finish it) or a
rewrite of the hold reason to "Left for you: - [ ] …" (only what a person must do; the hold keeps
its age). Each write re-checks the hold it judged (`holds.assessed_system_hold`). Triage-accepted issues missing from the board need no
rule: the reconciler adds every open issue (`reconcile_diff.membership`) and places a classified
one at Ready. The product's `needs_you_automation` policy key (`off | dry_run | on`, default `on`)
lowers the automation's mode for that product; `risk_tolerance` `balanced`/`bold` applies each
action (capped per tick: 10 closes, 20 moves, 20 releases, 20 prunes, 20 hold rewrites; audited
`pm.hygiene.<rule>`), `cautious` records an `OwnerRecommendation` instead. A refused write is
logged at INFO and retried no sooner than a day later. `GET /api/pm/recommendations` lists them;
`POST /api/pm/recommendations/apply` (`{ids, confirm: true}`) runs the same executor as the caller,
`…/dismiss` drops one for good. Each tick also sets the `viberpit_pm_needs_you{product}` gauge and,
at most hourly, writes an `AttentionSample` with the map's own numbers
(`pm_tree.product_attention_counts` — the badge count, `tree.needs_you_total`, and the waiting
counters); `GET /api/pm/attention/history?product=&days=` serves the trend.

**Parked review** (design §4, rule `parked_review` of the same automation). Parked issues (the
`parked` label; the queue skips them) are reviewed weekly per product, and daily while more than
the policy's `parked_review_threshold` (default 20; 0 = weekly only) are parked. A run covers up to
15 parked issues, the least recently reviewed first, at most 5 helper calls per tick fleet-wide
(the run's pending refs persist as the automation's `parked-review:<slug>` target state). For each,
a stateless helper (`server/automations/parked_reviewer.py`, the close-out assessor's
`ask_helper` and `merged_prs`) reads the issue and the merged PRs that reference it and answers one
verdict (`server/pm/parked_review.py`): `close_fixed` (must cite one of those merged PRs),
`close_obsolete` (with evidence), `unpark` or `keep`; an unusable reply decides nothing. Each verdict
is stored per issue (`parked:<ref>`, keyed to the body fingerprint). A non-`keep` verdict is ALWAYS
an `OwnerRecommendation` — whatever the product's `risk_tolerance`, and the executor refuses a
parked-review action that arrives via `automation` — so a parked label a person placed is only
removed, or the issue closed (`completed` for fixed, `not_planned` for obsolete), when the owner
approves; each write first re-checks the mirror still shows the issue open and parked, and an unpark
explains itself on the issue (marked as automation's) before removing the label as spelled.

**Owner digest** (design §5). `GET /api/pm/digest?product=` lists every item that truly needs
the owner, grouped by product, each with at most ONE recommended action
(`server/pm/digest.py`; composition in `server/api/pm_digest.py:load_digest`): held issues from
the escalation read model (a `model_approval` recommends approving the requested tier; a
checklist, a decision or a hand-off recommends nothing), the map's other needs-you issues (needs a
spec, a session waiting on a person, assigned to the owner) and initiative gates (never
recommended — no auto-advance; the item links to the gate), pending decisions not already held on a
board, triage proposals (accept recommended only at `high` confidence onto a capability), and
pending `OwnerRecommendation`s (their own action, with any parked-review evidence links; one
targeting an issue the digest already lists stands in for that item). `POST /api/pm/digest/apply`
(`{ids, confirm: true}`; 422 without confirm; "approve all" passes every id with a recommendation)
re-derives each id's action from current state and runs it through the path that owns it —
`escalation_approval.approve_escalation`, `pm_hygiene.apply_pending` (the recommendations
executor), `triage_accept.apply_accept` — each with its own audit, plus one `pm.digest.apply`
row per call; per-id `{id, ok, step, error}`. Once a day per product (when its digest is non-empty
and its `needs_you_automation` is not `off`, never in quiet hours) the hygiene tick sends one
`decision_needed` push — "N items need you — M have a recommendation" — linking to the product's
Needs-you tab, deduped on its `pm.digest.sent` audit row. The **Today's digest** card leads the
product and portfolio Needs-you tabs: each item with its recommendation as a button, evidence links,
and **Approve all recommendations (N)** behind a dialog that lists each action. The assistant reads
it with `get_owner_digest` and, operator tier only, applies it with `apply_owner_digest`.

**Held issues** lead the product and portfolio Needs-you lists, read from the escalation read
model (`GET /api/pm/holds`) rather than from the map's per-capability counts — an unclassified
held issue has no capability row to appear under, so without this list it surfaced only as a
"need a home" count. Each card names the issue (title, GitHub link), says in plain language who
parked it and why and what ends it (`features/products/vocab.ts:holdExplanation`, the same text
the Review-and-decide dialog shows), and carries the taps that release it, each bound to that
card's own work item: **Retry**, **Approve escalation to <tier>** on an approval-gated hand-off,
**Review and decide**, **Edit on GitHub**, and the board-move wording; a `decision` hold links to
its Inbox proposal instead of offering Retry. Each held row carries `attentionKind`
(`escalation.attention_kind`): `model_approval` (an approval-gated climb), `checklist` (a hold a
close-out assessment narrowed to "Left for you: …", rendered as a list), `decision` or
`owner_action`; the card's badge says which ("Model approval", "Checklist for you", …).

Click a summary tile to open its corresponding work list. Issues needing input appear before
blocked and other open issues. An issue that needs a spec explains that its description needs
clearer requirements and acceptance criteria; blockers retain their Remove action, and rows
associated with a session offer **View sessions**. The **Review and decide** panel shows the full issue in ViberPit. **Edit details** saves its
title and description; **Mark resolved** and **Won’t do** close it with the corresponding
GitHub reason after an explicit confirmation. Closing a report does not repair the underlying
account or code. Writes use the shared GitHub transport and journal; the local mirror changes
only after GitHub confirms success. A stale issue prompts a reload, and a failed save preserves
the draft. The GitHub link remains available as a secondary destination. A **Live** capability can still have outstanding work requiring a decision.

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
`proposed` leaves, and the inbox's oldest untouched proposal. Capability evidence (#2162): a doc's
`code:`/`tests:`/`specs:`/`adrs:` frontmatter lists (also read out of the older
`links: {code: [...]}` dict) index into `feature_evidence`; pm-sync resolves each path — file,
`dir/`, or glob (`server/pm/evidence.py`) — against the default-branch tree every pass, and the
readout adds dangling cited paths, `live`/`delivering` capabilities with no `tests:`, `code:` with
no `specs:`, and a doc (so its `state`) last committed before its newest linked spec.

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
Capability**. A capability's Dependencies tab carries the links chip editor (`CapabilityLinksEditor`, one
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
(constitution VI) — has thirteen fields: `dispatch`
(`off | propose | auto`, default `off` — whether the runner works this product's board at all, and
whether a claimed batch is only announced or actually spawned), `queue_min_status` (default `Ready`,
or `Backlog` to reach untriaged items — how far down the board the runner reaches), `max_in_flight`
(default 1, minimum 0 — this product's concurrent runner sessions, enforced per PRODUCT in the
runner's refill loop rather than translated into a per-repo cap, so a product owning several repos
still runs at most this many sessions total; a `proposed` announce-only claim holds the slot exactly
like a spawned one, a `gave_up` or `handed_off` claim does not. `0` means PAUSED — the product takes
no claim while keeping the rest of its policy — never "unlimited"), `max_pipeline_prs` (default blank = unlimited, minimum 0 — the product's **pipeline cap**: the most claims it may have in its pipeline at once (live sessions plus unmerged PRs handed off to PR Doctor); a full pipeline refuses new claims, spec 2026-09-20 D13), `ladder_top`
(enum over the model tiers, default `fable` — the strongest model the runner climbs to on a
repeated bounce; a bounce at this tier hands the issue to the owner instead of climbing further.
Replaced `max_attempts` — there is no fixed attempt cap any more (spec 2026-09-18 §4); see
`docs/product/features/autopilot-backlog-runner.md` § The model ladder), `give_up_after_bounces`
(default 0, minimum 0 — landed in phase 3, D8: the bounce threshold both the queue's `needs_spec`
skip and the roadmap's `needs_spec` reason compare against through the one predicate
`server/pm/outcomes.py:bounced_out`; `0` — the default since the model ladder landed — means never
give up, leaving the ladder's own length as the cap, and a non-zero value is an optional stricter
stop that makes the runner hand the issue to its owner at that bounce instead of writing a rung
hold the queue would then refuse. Editing the issue's title/body changes its `body_fingerprint` and
restarts the count — see "Dispatch outcomes" below),
`default_model` (default
`auto` — the session model; `auto` assesses each job), `respect_dependencies` (default `true`), and,
landed in phase 2, `triage_auto_accept` (`off | high | medium`, default `off` — the PM Triage loop's
own accept gate, at or above this confidence; `medium` includes `high`; see "Triage auto-accept"
below). Three more landed with roadmap auto-execution phase 1 (spec
`docs/superpowers/specs/2026-09-14-roadmap-auto-execution-design.md`), governing the SDLC front
chain rather than the Backlog Runner: `risk_tolerance` (`cautious | balanced | bold`, default
`cautious` — how far the front chain may resolve its own stage gates; `cautious` parks every gate
for a human exactly as before), `max_initiatives_in_flight` (default 2, minimum 0 — this product's
concurrent spec/plan artifact sessions, counted as LIVE design/plan artifact sessions only, so
intake, a `blocked` initiative, a dead session and a PR awaiting a human merge each hold no slot;
`0` PAUSES new artifact sessions without stopping materialization) and `unattended_model_floor`
(`none` or a model tier, default `none` — the floor for a session whose output ships unread; stored
in phase 1, applied in phase 2). `session_assistance` (`on | off`, default `on`, #1611) is the
product's say over the Autopilot automation: `off` drops the product's sessions from
`_autopilot_list_governed` (resolved through `assistance_withheld_repos`, the unique-owner
lookup), so Autopilot neither answers, approves, nudges nor archives them. It only restricts:
`on` leaves the global Autopilot mode, guardrails and Repositories scope in charge, and a repo
no product uniquely owns stays under that global scope. An unreadable policy skips the tick
rather than act on a product that may have said off. The minimums are `ConfigField.min`, enforced in `validate_config` (a below-minimum int is a 422
`bad_policy`, not a browser courtesy) and published in the schema JSON so `ConfigInput` renders the
same floor on the number input. `effective_policy`
never raises: a `None` row (never opted in) and an unparseable one both read as the pure defaults,
i.e. `dispatch: off` and `triage_auto_accept: off`.

It is edited from the product's **Automation** tab (`web/src/features/roadmap/detail/AutomationPolicyCard.tsx`,
mounted by `ProductDetail.tsx` at `?tab=automation`), a card that renders every schema field through the same `ConfigInput` component the
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
primary repo's entry in the old `repo_caps` becomes its `max_in_flight` (`repo_weights` is not
carried over — `weight` was removed, spec 2026-09-20 D6) — or, for a product with no repo flagged
primary, its lowest-named repo's entry — and the remaining
globals (`queue_min_status`, `default_model`, `respect_dependencies`) are copied onto every seeded
product, but only the keys the legacy config actually set: a key it left unset is left at the schema
default rather than pinned to a made-up value. `max_attempts` is NOT among them — it was retired in
favour of `ladder_top` and the model ladder (spec 2026-09-18 §4), and a tier ceiling synthesized
from a legacy attempt count would be invented intent, not a migration. A non-empty
`enabled_repos` seeded exactly the products owning those repos; the empty list — which meant "every
onboarded product" — seeded every product with a linked board. The seed CONSUMES its own input: in
the same transaction it strips every retired key off the runner row's config and normalises a stored
`propose`/`auto` mode to the phase-1 `on`, and a row carrying none of those keys is what makes the
next call a no-op. That marker is what makes it safe to run on every process start rather than
exactly once in history — durably, unlike the earlier "does any product have a policy" check, which
re-armed the moment an operator cleared the last policy. A runner-card config save strips those keys
too (the config PATCH validates against the phase-1 schema); a row left that way is never seeded
from — its repo list is gone — but its legacy mode is still folded to `on`. A legacy value below a
field's phase-1 minimum (a negative per-repo cap) costs that one field its legacy
value, never the whole policy. A product that already carries one is
skipped individually, so a human-set policy is never overwritten and never blocks the rest of the
migration.

What stays global on the runner: whether it runs at all (`off | on` — one row-level toggle;
propose-vs-auto is now the per-product `dispatch` value, not a runner mode), `selection_strategy`
(how a freed slot chooses among the ON products' repos — balanced / priority /
concurrency-cap), `max_concurrent` (sessions in flight across every product combined),
`max_spawns_per_day`, `max_issues_per_pr`, and `grouping_interval_minutes` (the give-up cooldown,
`needs_human_retry_days`, is retired — a give-up now ends only on the hold-release signals in the
Holds paragraph above, not a timer).

Phase 2 of the design has landed: confidence-tiered triage auto-accept per product
(`triage_auto_accept`, §8 — the "Triage auto-accept" paragraph below). **Phase 3 has landed**: the
`dispatch_outcomes` table feeding the bounce-based `needs_spec` skip and roadmap tier reason
(`give_up_after_bounces`, §6–7 — the "Dispatch outcomes" paragraph below). **Phase 4 has landed**:
a history-informed floor on the runner's model pick (§7, D9) — `outcome_stats`/`tier_floor` in
`server/pm/outcomes.py`, consumed by `server/automations/model_pick.py:pick_model` at spawn (see
`docs/product/features/autopilot-backlog-runner.md` § Model pick). This closes #1521's dispatch-
outcome feedback loop: the same `dispatch_outcomes` rows now feed the queue's bounce skip, the
roadmap's needs-spec chip, AND the model pick, so a real merge or bounce changes what the runner
does next on three fronts, not one. See
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
swallowed, never blocking a spawn or a sweep — and a spawn-time write that was lost that way is
RECOVERED at finalisation: with no `running` row and nothing to upgrade, `finalize_outcome`'s
`fallback` (the claim's own `product_slug`/`session_id`/`model`/`attempt_no`, passed by the loop's
`_outcome_fallback`) inserts the already-finalised row itself, fingerprinted off the mirror row and
with `started_at` left `None` because nobody knows when that dispatch began. It inserts at most one
row per dispatch — a target that already has a row for the same `session_id` is left alone, so the
sweep re-running its "ended with no PR" branch on later ticks cannot mint a bounce each time.
`record_outcome` inserts the `running` row right after
a spawn succeeds — from the loop's own `_refill` (`server/automations/backlog_runner.py`, guarded by
its `_record_outcome` wrapper) and from the two manual taps,
`SessionOrchestrator.backlog_runner_implement_core` (the propose tap) and `.backlog_runner_dispatch`
(manual dispatch) — and it first closes any row still `running` for that target as `superseded`
(`cause=None`; the outcome column alone carries the fact), so at most one `running` row exists per
target even when a tap races the loop's own claim.

`finalize_outcome` closes the row at whichever branch of `BacklogRunnerLoop._sweep_one` ends the
dispatch: a merged PR with a clean close-out → `merged` (with `first_pass_merge`); a merged PR that
still needs a human close-out → `needs_human`; PR Doctor giving up → `pr_gave_up`; a PERSON closing
the PR unmerged → `abandoned`; the session ending with no mergeable PR → `no_pr` (a bounce — the row
still reads `no_pr` even when that bounce lands at the model ladder's top and hands off to the
owner, spec 2026-09-18 §4, since the recorded OUTCOME was still "no PR"; see
`docs/product/features/autopilot-backlog-runner.md` § The model ladder); the session never having
run a single turn → `abandoned`. There is no fixed attempt cap any more: a bounce climbs the model
ladder (the `ladder_top` policy field, above) instead of counting toward one. The one surviving
`upgrade_from=("no_pr",)` fires on the session-never-ran branch: rather than leaving a stray second
row for one session, `finalize_outcome` re-finalises the newest matching `no_pr` row in place
(`ended_at`/`duration_s` kept, outcome/cause replaced) — one dispatch, one row.

`first_pass_merge` is `True`/`False` only when PR Doctor's recovery count for that PR is knowable at
the moment the merge is swept, and `None` otherwise — never guessed. The seam is
`SessionOrchestrator._backlog_pr_doctor_attempts_for`: an unknown target (no PR Doctor state row at
all) answers `None`; a tracked one answers `total_attempts + total_mechanical` (PR Doctor's
monotonic, re-arm-immune counters) plus `lifetime_attempts + lifetime_mechanical` — the same counts
carried forward by `SessionOrchestrator._pr_doctor_lifetime_carry` across BOTH of PR Doctor's state
clears: the CI-recovery clear (`_on_pr_change`, triggered when checks go back to passing) and the
terminal clear (`_pr_doctor_step`, when the PR merges or closes — the very moment the runner's sweep
asks the question). Both clears still reset
`total_attempts`/`total_mechanical` to zero, because the give-up ceiling in
`server/automations/pr_doctor_engine.py` reads exactly those keys and a recovered PR earns a fresh
budget — but without the `lifetime_*` carry (which the engine never reads, and which `apply_rearm`/
`record_attempt` preserve through every later cycle because both copy the whole state dict) a
doctor-rescued PR's history would be erased right before the runner's sweep asks whether the merge
was a first pass, and every doctor-rescued merge would misreport `True`.

**Readers (phase 3–4).** Beyond the bounce rule below, `server/pm/outcomes.py` exposes the SAME
rows to two more consumers. `outcome_stats(db, project_id, feature_id)` gathers one capability's
finished dispatch history per model tier — finalised, non-superseded rows whose `model` maps to a
tier via `model_catalog.model_tier` (an unranked model, e.g. `default`/`best`, is dropped rather
than guessed), counting each as an outcome and, when `first_pass_merge is True`, also as a first
pass. A `merged` row whose `first_pass_merge` is NULL (the attempt read answered UNKNOWN) counts in
NEITHER — unknown is evidence of neither success nor failure, and counting it as an outcome with no
first pass would read a tier that did merge as one that cannot do the work; every other finalised
row still counts as an outcome. `tier_floor(db, project_id, feature_id)` (pure `tier_floor_from_stats` underneath, D9) turns
that into the phase-4 model-pick floor — see
`docs/product/features/autopilot-backlog-runner.md` § Model pick for the rule and how
`_backlog_spawn` consumes it via `tier_floor_for_dispatch`.

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
`server/tests/test_pm_codex_live.py` (opt-in, `VIBERPIT_TEST_CODEX_LIVE=1`).

Both origins also carry the fleet-wide `default_model` setting every spawning automation has: one
enum per origin (`default` — the account's own model — through `auto` and each catalog alias), so
`pm-onboard`'s synthesis and critic share a tier and `pm-classify`'s chunk sessions get their own.
The value is resolved at the spawn site by `SessionOrchestrator._resolve_spawn_model`, the ONE
place that branches on engine: a Claude launch gets a catalog alias in `model=`, a Codex launch gets
a reasoning effort from that account's live catalog in `reasoning_effort=` (never a Claude alias —
#1289), and every failure path falls open to the account default rather than blocking the spawn.

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
`/triage` accept/reject/accept-bulk/reject-bulk/undo, `/work-items/{id}/overlay`);
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
