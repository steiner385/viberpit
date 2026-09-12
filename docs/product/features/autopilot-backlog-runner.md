---
slug: autopilot-backlog-runner
parent: planning-and-automation
title: Autopilot & Backlog Runner
state: live
links:
  code:
    - server/automations/autopilot.py
    - server/automations/autopilot_decider.py
    - server/automations/autopilot_policy.py
    - server/automations/backlog_runner.py
    - server/automations/backlog_selection.py
    - server/automations/backlog_grouping.py
    - server/automations/backlog_github.py
    - server/automations/product_policy.py
    - server/pm/outcomes.py
    - server/automations/model_pick.py
    - server/automations/action_providers.py
    - server/automations/consolidation_engine.py
    - server/automations/completion_bar.py
    - server/api/automations.py
    - web/src/features/delivery/automation/AutomationDetail.tsx
    - web/src/features/delivery/automation/actions.tsx
  prs: [610, 618, 626, 645, 575, 640, 683, 786, 550, 469]
---

# Autopilot & Backlog Runner

Keep eligible backlog work progressing within explicit budgets and governance while yielding promptly to human steering.

**Problem** — Running autonomous development on ViberPit means two distinct, tedious jobs
that a human would otherwise babysit around the clock: (1) *keeping live sessions moving* —
a session stalls waiting for a permission prompt, gets stuck in a nudge loop, opens a PR but
never arms auto-merge, or declares itself "done" with no PR to show for it; and (2) *feeding
the loop new work* — picking which open issues to implement next, grouping related ones,
claiming them so two sessions don't collide, spawning implementation sessions, and closing
the issues out only when a real linked PR merges. Done by hand this does not scale and does
not survive overnight; done naively it burns account quota, double-works issues, or
auto-closes things that actually needed a human.

**Target users** — The operator who wants the fleet to make progress unattended, and who
still wants to step in and steer any single session at will. Secondarily the other
automations that share the same registry/action surface.

**Desired outcome** — Autopilot advances each governed session toward its goal (approves
safe prompts, answers, nudges, escalates, or marks complete) while *deferring to the human*
the moment they start steering, refusing destructive commands, respecting per-session and
global daily action budgets, and never declaring victory without a merged-or-armed PR when a
PR is required. The backlog runner keeps a bounded number of implementation sessions in
flight, chosen fairly across repos, from a pre-computed grouping of the open backlog, with
atomic issue claims that self-heal, and closes issues only after fail-closed PR↔issue
verification. Both expose a manual "do this now" action surface for the operator.

**Success metrics** — (proposed, no baseline recorded)
- Sessions advanced to a merged PR without human intervention vs. total spawned.
- Nudge-loop / double-act incidents (target: caught by loop detection + single-lock, ~0).
- Wrongful auto-closes (target 0 after the #784/#786 human-gate; action-required and
  docs-only-satisfied issues escalate to needs-human instead).
- Per-repo fairness: no single repo's backlog monopolizes all slots under the balanced
  strategy.
- Quota respected: actions charged against per-session hourly and global daily caps; no
  spawn storms (per-target backoff + disk-exhaustion circuit breaker hold).

**Non-goals** — Autopilot does not write code or open PRs itself; it *steers* the claude
sessions that do. It does not override the destructive-command deny-list or the human-
deference window — those are hard guardrails, not decider suggestions. The backlog runner
does not invent a grouping at spawn time; if no persisted plan exists it waits and nudges a
refresh (single-writer discipline, constitution VI). Issue *closing* is gated on verified
PR linkage, never on labels alone.

**Notes** —
- Autopilot is a two-tier decider: pure synchronous hard rules (`autopilot_policy.py`) gate
  before and after a one-shot LLM decider (`autopilot_decider.py`, Haiku via the Claude Agent
  SDK, injected as a seam so tests never hit the model). Pre-decide enforces human deference
  (stand down ~120s after user input, #575), the destructive deny-list, per-session hourly
  budget, loop detection, and recovery caps; post-validate stops the untrusted decider from
  overriding a deny or firing a premature "complete" (#639, #640) and downgrades it to a
  composition-marked nudge. A 5-consecutive-failure counter escalates decider outages as
  errors, not blips.
- `completion_bar.py` is the single source of truth for "is the goal met?", used by both the
  live gate and the retro archive audit. Precedence: merged PR > armed PR (green CI; red
  blocks, #639) > open-unarmed PR (nudge to arm) > pr_resolution claim > explicit
  `--squash --auto` instruction > no-PR-nudge; research/Q&A goals may complete with no PR.
  Manual unarchives are respected and learned from (#469).
- Backlog runner (`backlog_runner.py`) refills slots under a single lock by
  select → claim → spawn, and sweeps for merged PRs (close-out), handoff readiness (free the
  slot once auto-merge is armed), and terminal sessions (charge an attempt, give up at max).
  Candidates come from the **global priority queue** over the mirrored Projects v2 boards
  (spec 003 §7a, `server/pm/queue.py`) — the same ranking `GET /api/pm/queue` renders, so
  what the operator sees is what agents do next.

  **Scope, reach and per-product knobs are the products' now** (product automation policy
  phase 1 — `docs/superpowers/specs/2026-09-12-product-automation-policy-design.md`,
  `docs/product/features/product-management-layer.md` § Product automation policy): each
  `Project.automation_policy` (`server/automations/product_policy.py`), not the runner's own
  config, decides. `_backlog_query_queue` keeps only products whose policy `dispatch != off`
  (a product that never opted in is `off` and contributes nothing), and ranks each ON product
  with ITS OWN `queue_min_status` (`Ready`, the safe default, or `Backlog` to reach untriaged
  items) and `respect_dependencies` (dependency-gated from the compiled graph: native
  blocked-by edges, `Depends on #N` body references and epic list order), merging the
  per-product rankings into one pool by the queue's own `rank_key` (score, then age, then issue
  ref) — never a hand-copied tie-break — so a cross-product tie between two unscored issues breaks
  by age, not by product slug. `weight` comes from each ON product's policy, applied to every repo
  it owns and handed to the selection strategies below as a per-repo weight/order (never a cap).
  `max_in_flight` is enforced directly per PRODUCT in the loop's `_refill`, counted once per tick
  from the runner's durable target-state rows and kept in step with every claim taken that tick: a
  `proposed` (announce-only) claim holds its product's slot exactly like a spawned one, while a
  `gave_up` or `handed_off` row does not (one predicate, `backlog_runner.is_in_flight`, shared with
  the fleet-wide `max_concurrent` count so the two ledgers cannot disagree about a row) — it is
  never translated into a per-repo cap, so a product owning several repos cannot run more than its
  own cap across all of them combined. Every cap is read literally, `0` included: `0` is the
  PAUSED setting (the product takes no claim but keeps the rest of its policy), not an
  "unlimited" sentinel, and the schema's `min: 0` keeps a negative value — which `_refill` would
  read as an unreachable ceiling — out of the store in the first place. The
  runner's own `mode` is just `off | on` now — whether it runs at all; dispatch mode (`propose`
  announces a batch without spawning, `auto` spawns) is a PER-PRODUCT decision
  (`policy["dispatch"]`), so one enabled runner can announce for a product still being trusted and
  spawn for one that's earned auto, on the same tick. `max_attempts` and `default_model` are read
  the same way, off the target's recorded product (falling back to the pure defaults with
  `dispatch: auto` for a claim written before the flip, or a product the policy map has lost — the
  queue is the real off-gate, so anything that reached the loop this far was already decided to
  run). A policy read that fails outright (the injected `read_policies` seam,
  `_backlog_load_policies`, raises) keeps the LAST
  good snapshot for that tick's refill if there is one, or skips refill entirely with no snapshot
  yet — either way the sweep (handoffs, give-ups, reconciliation) still runs, since stalling that
  on the same DB blip would be its own outage. The runner's OWN config schema keeps only
  fleet-wide knobs: `selection_strategy`, `max_concurrent`, `max_spawns_per_day`,
  `max_issues_per_pr`, `grouping_interval_minutes`, `needs_human_retry_days`.
  `retired_config_keys` (`eligible_label`, `enabled_repos`, `repo_weights`, `repo_caps`,
  `queue_min_status`, `max_attempts`, `default_model`, `respect_dependencies`) still lists the
  retired ones so a row that still carries them keeps the rest of its settings; on read, a stored
  `off` mode survives verbatim (an operator who parked the runner is never silently restarted), a
  stored `propose` or `auto` mode (written before the flip) folds to `on`, and an unset (`NULL`)
  mode on an otherwise-enabled row also reads as `on` (the two-mode UI hides the mode select, so
  its toggle alone must be enough to start the row) — the flip's one-shot seed
  (`server/sessions/orchestrator.py`'s `_backlog_seed_policies`, run once per daemon process on
  the loop's first tick, but only for an ENABLED row carrying a stored mode — a never-configured or
  disabled row has nothing to migrate) is what turns that old value, and the old `enabled_repos` /
  `repo_weights` / `repo_caps` / the rest, into each owning product's policy instead — a product's
  primary repo's entry in `repo_caps` / `repo_weights` becomes its `max_in_flight` / `weight`, or
  its lowest-named repo's when none is flagged primary, and only the keys the legacy config
  actually set are written, the rest left at the schema default — so no product's behaviour
  changed at the flip. The seed is one-shot because it CONSUMES its own input: in the same
  transaction that writes the policies it strips every `retired_config_keys` key off the runner
  row's stored config and normalises a stored `propose`/`auto` mode to `on` (so the row an
  operator sees in `GET /api/automations` stops claiming a mode the UI no longer offers), and a
  row carrying none of those keys is what makes the next call a no-op. A product that ALREADY has
  a policy is skipped individually — a human's answer is never overwritten, and their edit does
  not strand the other products on the wrong side of the migration. The status
  card appends one ` · `-joined segment per product whose dispatch is on, onto the SAME status-card
  line ("slug: dispatch · in_flight/cap in flight · eligible eligible") rather than a separate line
  each (the web renders `AutomationStatus.text` without honouring newlines); it reports "idle — no
  product has dispatch on" when every product's is off, or, when the per-product count read itself
  failed, "N implementing · product counts unavailable (the queue read failed)" rather than either
  — "we don't know" and "no product is opted in" are different sentences on the card.

  **Dispatch outcomes (phase 3, spec §6/§7).** Every dispatch leaves one durable
  `dispatch_outcomes` row: the loop's own spawn in `_refill`, and the two manual taps
  (`SessionOrchestrator.backlog_runner_implement_core`, `.backlog_runner_dispatch`), each record
  a `running` row (`AutomationContext.record_outcome`) right after the spawn succeeds, with the
  attempt number and model the session actually got. `BacklogRunnerLoop._sweep_one` finalises it
  (`AutomationContext.finalize_outcome`) at whichever terminal branch closes the target: `merged`
  (carrying `first_pass_merge`), `needs_human` (a merged PR still needing a human close-out, or
  the attempt cap reached with a real attempt behind it), `pr_gave_up` (PR Doctor gave up),
  `abandoned` (the PR closed unmerged, or the cap was reached by a session that never ran a
  turn), or `no_pr` (the session ended with no mergeable PR and attempts still left — a bounce).
  Both attempt-cap branches finalise with `upgrade_from=("no_pr",)`: the cap is discovered a
  sweep after the bounce that carried the dispatch to it, so the row that closes is the earlier
  `no_pr` row, re-finalised in place, not a second row for the same session. A second dispatch
  over a still-`running` row (a double tap racing the loop) closes the earlier row `superseded`
  instead of `abandoned` — it never got to answer, so it is not counted as a bounce. Both writers
  are best-effort (`server/pm/outcomes.py`, spec §13): a write failure is logged and never blocks
  a spawn or strands a claim mid-sweep — and a spawn-time write lost that way is recovered by the
  sweep, which passes the claim's own product/session/model/attempt (`_outcome_fallback`) as
  `finalize_outcome(fallback=…)` so the dispatch is inserted already finalised rather than
  forgotten, at most once per dispatch.

  **Model pick (phase 4, spec §7, D9).** A product's `default_model` policy field of `auto`
  still asks `assess_model` (one-shot Haiku complexity judgment) for a tier per dispatch; what's
  new is that the capability's own merge history can now RAISE that assessment. `_backlog_spawn`
  reads the floor through an injectable seam, `_backlog_tier_floor` (`orchestrator.py`), which
  opens its own `db_session()` and defers to `server/pm/outcomes.py:tier_floor_for_dispatch` for
  the primary issue's product/capability; the read is best-effort by spec §13 — a failed lookup
  (unknown product, an unmirrored or unparseable ref, an issue on no capability, or a DB error) is
  logged and costs only the floor, never the spawn. `tier_floor_from_stats` is the one D9 rule
  (pure, ranked by the catalog's single `TIER_ORDER`, never a second list or string compare): the
  floor is the lowest tier at which that capability's issues merged first-pass in ≥ 2 finalised,
  non-superseded outcomes, when every cheaper tier with ≥ 2 such outcomes merged first-pass in
  none of them (and at least one such cheaper tier exists) — a tier with fewer than two outcomes
  is not evidence in either role. `server/automations/model_pick.py:pick_model` is the one
  resolver (`resolve_model` is its two-tuple wrapper): the floor only ever RAISES an `auto`
  assessment, compared by `model_catalog.tier_rank`, never lowers one, and a pinned alias or the
  account `default` is untouched — the source recorded is `"auto+floor"` when the floor actually
  raised the pick, `"auto"` otherwise. The spawned `AutomationEvent`'s `detail` carries
  `assessed`/`floor` only when set (so a pinned spawn's detail is unchanged from before this
  phase), and every `auto`-policy pick — raised or not — also writes one
  `backlog_runner.model_pick` audit row (identity `backlog-runner`, `{target, product, assessed,
  floor, applied, source}`), so "the assessor said sonnet but this capability's history floored it
  at opus" is recoverable after the fact. Neither the event detail nor the audit surfaces in the
  UI yet — `AutomationContext.list_events` drops `detail` entirely, so this is data for the
  history, not the activity feed, until a later phase reads it.

  `_backlog_query_queue` threads each ON product's own `give_up_after_bounces` into `rank`
  (`server/pm/queue.py`), which checks the `needs_spec` skip right after the needs-human cooldown
  and before the board-status gate: a row whose body (by `server/pm/outcomes.py:body_fingerprint`)
  has bounced `give_up_after_bounces` times or more (`bounced_out`) is skipped `needs_spec`
  rather than ranked — the SAME predicate the roadmap's leaf chip reads (see
  `docs/product/features/product-management-layer.md` § Dispatch outcomes). The status card's
  per-product segment appends ` · N bounced` when `N` is nonzero (`BacklogRunner.status`), and the
  SAME per-product rows also ride structured on `AutomationStatus.products` (a new field beside
  `text`, never a sibling top-level key — `GET /api/automations` serializes it once, inside each
  row's `status`): `[]` when the runner has products but none has dispatch on, `None` for an
  automation with no concept of products (PR Doctor) or while the read has failed. The runner's
  detail pane renders it as a table on the Status tab whenever it is a non-empty array
  (`AutomationDetail.tsx`, via the shared `CheckTable` primitive): Product (linked to
  `/roadmap/:slug`), Dispatch, In flight (`inFlight/cap`), Eligible, Bounced.

  The top pick is re-verified against live GitHub immediately before the
  claim — that check **fails closed** (§10), so a mirror lagging reality costs a tick, never
  a session spawned on stale work. The old `eligible_label` gate is retired: board status,
  not a label, says what is workable.
  Grouping is a background Sonnet optimizer (#610) persisted in `backlog_groups`; selection
  ordering supports balanced / priority / weighted / concurrency-cap strategies (#773) for
  per-repo fairness (#771) — `priority` now serves the ON products' repos oldest-first by
  `Project.created_at`, the deterministic stand-in for the retired hand-ordered `enabled_repos`
  list. A **claim is the runner's durable target-state row** (#994): writing
  the row is what takes the issue out of the pool, the session it spawns is what the reconciler
  renders as board Status `In progress`, and the issue comment that follows is the claim's
  human-visible trace only — a failed comment cannot lose a claim. Reconciliation heals dangling
  claims (row, no session) by deleting the row, which IS the release; the GitHub-truth orphan
  sweep went away with the label search it diffed against (Phase D decision D-5), leaving the
  row and the board as the only claim state there is.
  A **give-up is the same row marked `gave_up` and stamped
  `needs_human_at`**, which replaces the claim (a target waiting on a human holds no slot): the
  reconciler turns it into board Status `Needs human` and the queue into a cooldown that expires
  after `needs_human_retry_days`, at which point the runner deletes the row (before refill, so
  the same tick can re-pick the issue) and the issue is retried — or never, when that setting is
  0. A human takes an issue back from a give-up by **dispatching it manually**
  (`POST /api/automations/backlog-runner/dispatch`, or the card's dispatch action), which
  replaces the give-up row with a fresh claim; moving the board card alone does not shorten the
  cooldown (follow-up #1540). Backoff + disk-exhaustion circuit breaker in #626/#683.
- The runner had to learn to see its own PRs (#546 sweep-time resolution) and to fail-closed
  on PR↔issue linkage before closing (#643), and not to auto-close action-required /
  docs-only issues (#784/#786).
- Consolidation (`consolidation_engine.py`) clusters same-scope open PRs into low-risk merge
  candidates for a human-driven consolidation session; PRs already being consolidated are
  flagged and linked (#555). Manual actions for all automations are enumerated by
  `action_providers.py` and driven through `server/api/automations.py`; the UI was the
  top-level Automations tab (#578), later folded into the Delivery section's automation
  detail (`/delivery/automations/:id`, `web/src/features/delivery/automation/AutomationDetail.tsx`
  + `actions.tsx`) with per-automation session model selection (#550).

**Evidence** —
- Autopilot core / decision: `server/automations/autopilot.py`,
  `server/automations/autopilot_decider.py`, `server/automations/autopilot_policy.py`,
  `server/automations/completion_bar.py`.
- Backlog work selection / claim / spawn: `server/automations/backlog_runner.py`,
  `server/automations/backlog_selection.py`, `server/automations/backlog_grouping.py`,
  `server/automations/backlog_github.py`.
- Per-product automation policy (phase 1 — scope, reach, caps/weights/attempts/model, the
  one-shot seed off the retired repo knobs): `server/automations/product_policy.py`,
  `server/sessions/orchestrator.py` (`_backlog_query_queue`, `_backlog_select_batch`,
  `_backlog_seed_policies`), `web/src/features/roadmap/detail/AutomationPolicyCard.tsx`;
  see `docs/product/features/product-management-layer.md` § Product automation policy and
  `docs/superpowers/specs/2026-09-12-product-automation-policy-design.md`.
- Claim-label retirement (#994): `server/pm/label_retirement.py`,
  `scripts/retire_claim_labels.py`, `specs/003-gh-projects-integration/phase-d-runbook.md`.
- Action surface / consolidation / API / web: `server/automations/action_providers.py`,
  `server/automations/consolidation_engine.py`, `server/api/automations.py`,
  `web/src/features/delivery/automation/AutomationDetail.tsx`,
  `web/src/features/delivery/automation/actions.tsx`.
- PRs: #610 (precomputed grouping optimizer), #618 (manual issue dispatch), #626 (spawn
  backoff + disk circuit breaker), #645 (contextual nudges, two-tier decider), #575 (human
  deference), #640 (armed-PR requirement for `--squash --auto`), #683 (per-target backoff
  retune), #786 (don't auto-close action-required / docs-only issues, fixes #784), #550
  (per-automation session model), #469 (never auto-archive a manual unarchive).
