# Product automation policy — design

**Status:** approved 2026-09-12 (Tony: opt-in = dispatch + triage auto-accept; policy as a JSON blob on `Project`; auto-accept under triage's existing `propose` mode; the remaining defaults as drafted). Written after the 2026-09-08 finish line closed, as the brainstorm Tony asked for on 2026-09-11: "a per-product automation policy (levers 2 + 3) as the opt-in replacement for the Backlog Runner". Decisions marked **[decided]** were confirmed by Tony; those marked **[default]** were drafted by the author and accepted without objection.

**Tracks:** #1521 (dispatch outcome feedback loop), lever 3 (confidence-tiered triage auto-accept) from the 2026-09-10 roadmap/triage/dispatch assessment. Supersedes nothing; extends `specs/003-gh-projects-integration/spec.md` §7a (the runner reads the board queue) and the roadmap drill-down spec's attention tier (§11).

## 1. Problem

Automation is global and the products are not. One `backlog-runner` row holds one mode, one queue floor, one attempt cap and one model choice for every onboarded product; the only per-product lever is a repo allowlist (`enabled_repos`) plus two dicts (`repo_weights`, `repo_caps`) on that same row. Turning the runner on for ghostdriver means turning it on for KinDash with KinDash's knobs. Triage proposes and never accepts, so every product's Backlog→Ready flow waits on a human even when the classifier said `high` confidence. And nothing learns: an issue that bounced twice looks exactly like one never tried — the roadmap shows a calm Backlog row, model pick guesses fresh, the queue retries after a blind timer.

Five products are onboarded today (viberpit `classifying`, kindash and ghostdriver `registered`, web `map_proposed`, kwgt-cairn `map_merged`); the runner is `auto` with 6 implementing, 256 eligible, 90 blocked, 45 untriaged, 6 needs-human; triage is `propose` with 146 to triage and 20 awaiting accept.

## 2. Decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | **A product opts in; the automations execute.** Each `Project` carries an `automation_policy`; the Backlog Runner and PM Triage loops read it per product and act only for products whose policy says so. The global automation rows keep only fleet-wide limits (concurrency, spawns per day, grouping cadence). | The runner's "which products" question is a product question. Fleet limits protect shared capacity and stay global. |
| D2 | **[decided] Opt-in governs dispatch AND triage auto-accept** (option b of the hand-back). The outcome loop (#1521) is built as the data layer under both, with its consumers phased. | Auto-accept is what lets Backlog→Ready flow without a human per product; outcomes need dispatch data that only exists once a product dispatches. The "whole loop incl. materialization" option is a follow-on (§11). |
| D3 | **The policy is a `ConfigField` schema, stored as JSON on `Project`.** `PRODUCT_POLICY_SCHEMA` reuses `ConfigField`/`validate_config` (`server/automations/base.py`) and the web's generic `ConfigInput` renderer. NULL = never opted in (today's behaviour for that product). | One config machinery, one editor. Constitution VI: no second schema/validator/renderer. |
| D4 | **Precedence: product policy over global config, for the knobs it names; fleet limits are global only.** `enabled_repos`, `repo_weights`, `repo_caps`, `queue_min_status`, `max_attempts`, `needs_human_retry_days`, `default_model`, `respect_dependencies` retire from the runner's global schema (retired keys stay readable for one release, then drop). | Two places for one knob is the bug shape this repo names first. |
| D5 | **A per-dispatch outcome is one appended row** (`dispatch_outcomes`), written at spawn and finalised by the runner's sweep — the one state machine that already observes session end, PR status and give-up. | Today's attempt counters are overwritten in place inside `AutomationTargetState`; the activity feed is not queryable by schema. History needs its own rows. |
| D6 | **Triage auto-accept is a tier gate on a proposal's own `confidence_level`, per product, never for no-fit proposals**, applied by the triage loop through the same accept function the API uses, moved out of the router. | `_apply_accept` hardcodes `classified_by = "human"` inside `server/api/pm.py`; an automated caller must share it, not copy it. No-fit proposals create capabilities — a human decision. |
| D7 | **Auto-accepts keep the undo window and are labelled.** `classified_by = "auto"`, audit `pm.triage.auto_accept`, inbox chip "auto-accepted · undo" for `TRIAGE_UNDO_WINDOW_S`. | The human safety net stays exactly where it is; only the default flips. |
| D8 | **"Bounced" is a product-level rule with one source of truth.** `give_up_after_bounces` (policy, default 2): an issue whose finalised outcomes for the product's current issue body reach the count gets queue skip reason `needs_spec`, roadmap tier `needs-you` with reason `needs_spec`, and is retried only after the issue body changes on GitHub. | Replaces the blind `needs_human_retry_days` timer for repeated failures; the fix is a spec edit, and editing is what restarts the clock. |
| D9 | **Model pick reads history as a floor, not a decision.** `resolve_model` gains a `floor` from the capability's outcome stats: when issues under the same capability merged first-pass only at a higher tier, the assessed tier is raised to it. | Evidence over a fresh guess (#1521 item 2) without replacing the assessor. |
| D10 | **Migration is mechanical and one-shot.** At first tick after deploy, the runner seeds a policy for every product that has a repo in the old `enabled_repos` (dispatch `auto`, the global knobs copied), and none for the others; then the retired keys are ignored. | No product changes behaviour at the flip; the operator sees the seeded policies on each product page. |

## 3. Use cases

1. **Turn on one product.** On kwgt-cairn's page, Tony sets dispatch to `auto`, queue floor `Ready`, 1 in flight, model `auto`. Only kwgt-cairn issues dispatch; KinDash stays off.
2. **Let triage flow for a mature map.** viberpit's capability map is stable; Tony sets auto-accept to `high`. High-confidence proposals land as classified with an undo chip for a minute; medium/low still wait in the Inbox.
3. **See why an issue keeps coming back.** ghostdriver#41 bounced twice (no PR, then PR Doctor gave up). The roadmap's Needs-you tab shows "bounced 2× — needs spec"; the queue skips it; Tony edits the issue body and the next tick reconsiders it.
4. **Stop everything for one product without touching the rest.** Set kindash's dispatch to `off`; in-flight sessions finish, no new claims.
5. **Read the fleet.** Delivery → Automations → Backlog Runner shows per-product rows: dispatch mode, in flight / cap, eligible, bounced.

## 4. The policy

`PRODUCT_POLICY_SCHEMA: tuple[ConfigField, ...]` (new, in `server/automations/product_policy.py`):

| key | type | default | governs |
|---|---|---|---|
| `dispatch` | enum `off | propose | auto` | `off` | whether the runner claims this product's issues; `propose` announces a batch without spawning (today's global propose mode, per product) |
| `queue_min_status` | enum (`MIN_STATUS_OPTIONS`) | `Ready` | how far down the board the runner reaches for this product |
| `max_in_flight` | int | 1 | this product's concurrent runner sessions (replaces `repo_caps`) |
| `weight` | number | 1 | this product's share when the fleet slot is contested (replaces `repo_weights`) |
| `max_attempts` | int | 2 | per-issue attempts before the runner gives up (today's meaning) |
| `give_up_after_bounces` | int | 2 | finalised non-merged outcomes on the same issue body before `needs_spec` (D8) |
| `default_model` | enum (`AUTOMATION_MODEL_CHOICES`) | `auto` | model for this product's sessions; `auto` assesses with the history floor (D9) |
| `respect_dependencies` | bool | true | today's meaning |
| `triage_auto_accept` | enum `off | high | medium` | `off` | accept proposals at or above this confidence (`medium` includes `high`); never `no_fit` |

Storage: `Project.automation_policy` (JSON, nullable) + `Project.automation_policy_updated_at`. Nullable, so `_reconcile_columns` adds them on both dialects; migration `0042_product_automation_policy` recorded anyway.

**[default]** `selection_strategy` stays global (`balanced | weighted | priority | concurrency-cap`) and reads `weight`/`max_in_flight` from the policies instead of the two dicts; `priority` order becomes the products' `updated_at`-independent order of `Project.created_at` unless a `priority` int is added later (YAGNI now).

## 5. The runner

- **Scope:** `_backlog_query_queue` keeps loading the board mirror for every onboarded product, then keeps only products whose policy `dispatch != off` (replacing the `enabled_repos` filter at `orchestrator.py:5107-5113`). A product without a policy is `off`.
- **Ranking:** `rank()` is called per product with that product's `queue_min_status`, `respect_dependencies`, `needs_human_retry_days` (kept global for now — **[default]**; it only governs the legacy needs-human cooldown, which D8 supersedes for repeated failures) and, new, the `needs_spec` skip (§7).
- **Batch:** `select_batch` / `order_live_groups` read `weight` and `max_in_flight` from the policies (`backlog_selection.py:85-95` shape unchanged: a mapping keyed by product slug instead of repo).
- **Propose vs auto:** the global runner mode becomes `off | on`; per product, `propose` announces the batch (the existing announce path) and records an `AutomationEvent` `proposed`, `auto` spawns. A `propose` product never claims.
- **Spawn:** `_backlog_spawn` resolves the model from the product's policy with the history floor (§8); records the outcome row `running` (§6).
- **Status card:** per-product lines ("kwgt-cairn: auto · 1/1 in flight · 12 eligible · 2 bounced"), replacing the single repo count line.
- **Retired keys:** `enabled_repos`, `repo_weights`, `repo_caps`, `queue_min_status`, `max_attempts`, `needs_human_retry_days` (**[default]** stays), `default_model`, `respect_dependencies` join `retired_config_keys`; `PATCH /api/automations/backlog-runner` rejects them with the existing retired-key message.

## 6. Dispatch outcomes (#1521 data layer)

Table `dispatch_outcomes` (migration `0041`, all columns nullable or defaulted; `_FK_LINKS` entries for `project_id → projects`, `session_id → sessions`):

| column | meaning |
|---|---|
| `id`, `created_at`, `updated_at` | |
| `automation_id` | `backlog-runner` (the only writer in v1) |
| `project_id` | the product the issue belonged to at spawn |
| `target_ref` | `owner/repo#N` — the queue's key (003 C-1 scope) |
| `body_fingerprint` | sha256 of the issue title + body at spawn (from the mirror row) — "same issue body" for D8 |
| `session_id` | the spawned session |
| `model` | the resolved model alias |
| `attempt_no` | the runner's attempt counter at spawn |
| `started_at`, `ended_at`, `duration_s` | |
| `outcome` | `running | merged | no_pr | pr_gave_up | needs_human | reverted | abandoned` — **phase-3 amendment:** plus `superseded` (a later dispatch replaced this still-`running` row before it could finish; not counted as a bounce) |
| `cause` | free text: the give-up reason / PR Doctor class / "session archived by human" |
| `pr_number`, `pr_url` | when a PR was found |
| `first_pass_merge` | bool — merged with no PR Doctor recovery attempt |

**Writers.** One insert at spawn (`running`). Finalised by `BacklogRunnerLoop._sweep_one`, which already sees: session ended without a PR → `no_pr`; PR merged → `merged` (`first_pass_merge` = PR Doctor `total_attempts == 0` for that PR); PR Doctor `GiveUp` → `pr_gave_up` with the failure class; runner give-up → `needs_human` with its reason; a human archiving the session or closing the PR unmerged → `abandoned`; a merge later reverted is out of v1 (`reverted` reserved). The sweep already holds the target-state row and the PR status; the change is one `finalize_outcome(...)` call per terminal transition, through the automation context so the fake records it.

**Reads.** `outcome_stats(db, project_id, feature_id=None, target_ref=None)` in `server/pm/outcomes.py` (pure query helpers, no loop logic): per-issue bounce count for the current fingerprint; per-capability first-pass merge rate by model tier.

## 7. Consumers

**Queue (D8).** `skip_reason` gains `needs_spec`: the issue's finalised outcomes with `body_fingerprint == current fingerprint` and `outcome ∉ {merged, running}` count ≥ policy `give_up_after_bounces`. The fingerprint is computed from the mirror row at rank time, so an edited issue body simply stops matching. Reported on the status card ("2 bounced").

**Roadmap (D8).** `issue_tier` gains a `reason` (the `InitiativeIn.reason` shape): `needs_spec` when the same rule fires → tier `needs-you`. `_leaf` carries `bounces` and `reason`; `Leaf` (web) gains both; `LeafList` and `NeedsYouList` render the chip "bounced N× — needs spec" with the initiative pattern (`reasonLabel`). The product's `counts.needsYou` rolls it up like any other needs-you issue.

**Model pick (D9).** `resolve_model(config, task_summary, floor=None)`: `_backlog_spawn` passes `floor = outcomes.tier_floor(project_id, feature_id)` — the lowest tier at which that capability's issues merged first-pass in ≥ 2 outcomes, when the lower tiers' first-pass rate was 0 across ≥ 2 outcomes. `assess_model`'s answer is raised to the floor; a pinned alias is untouched. Audit detail on the spawn records `{assessed, floor, applied}`.

## 8. Triage auto-accept (lever 3)

- `server/pm/triage_accept.py`: `apply_accept(db, proposal, *, decided_by: str, edits=None)` — `_apply_accept` moved out of `server/api/pm.py` verbatim, `classified_by = decided_by` (`"human"` from the routes, `"auto"` from the loop). The router imports it; nothing else changes for humans.
- `PmTriageLoop._run` gains one step after `ClassifyBatch` (and on every tick, so a policy change catches up): for each `pending` proposal whose product policy has `triage_auto_accept != off`, with `proposed_feature_id` set and `confidence_level` at or above the tier, call `apply_accept(decided_by="auto")`, record audit `pm.triage.auto_accept` `{proposalId, workItemId, featureId, confidence}` and `AutomationEvent` kind `auto_accepted`. Undo keeps working: the proposal's `undo_snapshot` is written the same way, and `POST /triage/{id}/undo` inside the window returns it to pending exactly as for a human accept. **[default]** An undone proposal is marked `auto_undone = true` and is never auto-accepted again — a human undid the machine once; the machine does not get a second vote.
- PM Triage's mode stays `off | propose`; auto-accept is a product policy, not a triage mode — `propose` means "the loop runs", and whether a product's proposals self-accept is the product's setting. **[decided]**
- Inbox: an accepted-by-auto proposal shows for the undo window with chip "auto-accepted" and the undo button; after the window it leaves the inbox like a human accept.
- Safety: per product, at most `max_auto_accepts_per_tick = 25` (a constant, not a knob); never no-fit; never a proposal whose target capability is `proposed` (unmerged doc).

## 9. UI

- **Product Overview** (`ProductDetail.tsx`, rendered once `onboarding_state == live`, else a note "Automation starts once the product is live"): an **Automation** card listing the policy via the generic `ConfigInput` renderer (moved from `features/delivery/automation/ConfigPanel.tsx` into `ui/` or a shared module — the same fields, the same inputs), saving through `PATCH /api/pm/projects/{slug}/policy` (new; validated by `PRODUCT_POLICY_SCHEMA`, audited `pm.project.policy`). A product with no policy shows the defaults with dispatch `off` and a one-line explanation.
- **Delivery → Automations → Backlog Runner detail:** a per-product table (dispatch, in flight/cap, eligible, bounced) built from the status payload; each row links to the product's Overview.
- **Roadmap:** the `needs_spec` chip on leaves and in Needs-you (§7); the Inbox "auto-accepted" chip (§8).

## 10. Data model and API summary

- `Project.automation_policy` JSON, `Project.automation_policy_updated_at` (nullable). `GET /api/pm/projects` and `/{slug}` include `automationPolicy` (or `null`). `PATCH /api/pm/projects/{slug}/policy` `{policy}` → validated, whole-object replace.
- `dispatch_outcomes` (§6). `GET /api/pm/work-items/{id}/outcomes` (the leaf's history; used by the chip's tooltip and the assistant).
- `TriageProposal.auto_undone` (bool, nullable) **[default]**.
- Backlog Runner status payload gains `products: [{slug, dispatch, inFlight, cap, eligible, bounced}]`.
- Retired runner keys per D4; `PRODUCT_POLICY_SCHEMA` exported by `GET /api/automations` beside the runner's schema so the web renders it with the same component.

## 11. Phasing

1. **Policy + runner scoping.** Model columns, migration, schema, `PATCH …/policy`, product Overview card, runner reads policies (scope, floor, caps, weights, model), one-shot seed from `enabled_repos` (D10), retired keys, per-product status lines. Behaviour-preserving at the flip.
2. **Triage auto-accept.** `triage_accept.py` move, the loop step, undo/`auto_undone`, inbox chip, audit.
3. **Outcomes + queue/roadmap consumers.** `dispatch_outcomes`, writers in the sweep, `needs_spec` skip and tier reason, leaf chips, status card counts.
4. **Model-pick floor.** `outcome_stats.tier_floor`, `resolve_model(floor=)`, spawn audit.

Not in this spec (follow-ons): materialization under the policy (option c), `reverted` detection, a per-product `priority` for the `priority` strategy, product-level notification preferences.

## 12. Testing

- Pure: `validate_config(PRODUCT_POLICY_SCHEMA, …)` defaults and rejections; `skip_reason` `needs_spec` with fingerprints; `issue_tier` reason; `tier_floor` over synthetic outcome rows; the auto-accept gate (tier ordering, no-fit, proposed capability, `auto_undone`).
- Loop: `test_backlog_runner_loop.py` gains per-product scoping/caps/propose-vs-auto cases through `FakeAutomationContext` (outcome writes recorded there); `test_pm_triage_loop.py` gains the auto-accept step with the undo round trip.
- API: policy PATCH validation + audit; projects payload; outcomes read; the accept routes unchanged (`decided_by="human"`).
- Postgres suite for the new table (`_FK_LINKS`, `_REQUIRED_EXTRAS` if any NOT NULL).
- Web: product Overview card (renders the schema, saves, disabled until live), leaf/needs-you chips, inbox auto-accepted chip; Playwright: set a policy on the product page and see the status line change.

## 13. Failure modes

- A policy write for a product without a linked board: accepted, but the runner's status line says "no board — nothing to dispatch" (the queue is board-scoped by C-1).
- The outcome write fails at spawn: the spawn proceeds; the sweep inserts the row at finalisation with `attempt_no` from the target state (history is best-effort, dispatch is not).
- Two products share a repo (not supported by onboarding today): the policy of the product owning the mirror row wins; logged once.
- Auto-accept when the classifier's confidence vocabulary drifts (a value outside low/medium/high): treated as `low`, never accepted.
- Seed at the flip finds a repo in `enabled_repos` with no product: ignored with a warn event (it was already dropped by C-1).

## 14. Decisions of record (2026-09-12)

Answered by Tony on 2026-09-12: (1) opt-in = dispatch + triage auto-accept (D2); (2) the policy is a JSON blob on `Project` through the `ConfigField` machinery (D3); (3) auto-accept is a product setting under triage's existing `propose` mode (§8). Accepted as drafted: `needs_human_retry_days` stays a global runner knob (§5); an undone auto-accept is never retried by the machine (§8); `weight`/`max_in_flight` are per product, not per repo (§4). Storage as a JSON blob is "fine for now" — typed columns remain an option if the policy grows.
