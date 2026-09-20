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
    - server/automations/ownership.py
    - server/automations/target_identity.py
    - server/automations/backlog_selection.py
    - server/automations/backlog_grouping.py
    - server/automations/backlog_github.py
    - server/automations/backlog_prompt.py
    - server/automations/product_policy.py
    - server/pm/outcomes.py
    - server/pm/holds.py
    - server/pm/error_family.py
    - server/pm/explain.py
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
- Backlog runner (`backlog_runner.py`) refills slots through shared ownership by
  select → claim → spawn, and sweeps for merged PRs (close-out), handoff readiness (free the
  slot after the implementation stops and effective PR recovery accepts ownership), and
  terminal sessions (climb the model ladder one tier on a bounce, hand off to the owner once
  the ladder tops out).
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
  per-product rankings into one pool by the queue's own `rank_key` (urgency, then score, then age,
  then issue ref) — never a hand-copied tie-break — so a cross-product tie between two unscored
  issues breaks by age, not by product slug. **Urgency leads** because the other legs work against
  it: an issue an automation files to report a broken environment (`URGENT_LABELS`, today just
  `deploy-monitor`) is unscored and brand new, so score-then-age sorted it to the *back* of the
  queue at the moment it mattered most — two incidents reporting both KinDash admin environments
  down ranked 328th and 329th of 332 (#16150, #16151). The grouping plan applies the same
  invariant on its own side (`parse_group_plan` floats a group holding urgent work to the front),
  because the plan — not the pool — is what the runner pops, and both of its ordering paths
  otherwise undo it: the model is free to rank an incident last, and the degraded all-singletons
  path sorts by issue number, where the newest issue is always last. Urgency reorders only; it
  never overrides a product's `dispatch: off`, its `max_in_flight`. The `priority` strategy (and its deprecated alias `weighted`, spec 2026-09-20 D6) serves each ON product's repos in rank order (`priority_rank`, then product age).
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
  spawn for one that's earned auto, on the same tick. `ladder_top` and `default_model` are read
  the same way, off the target's recorded product (falling back to the pure defaults with
  `dispatch: auto` for a claim written before the flip, or a product the policy map has lost — the
  queue is the real off-gate, so anything that reached the loop this far was already decided to
  run); `ladder_top` additionally gets clamped every tick to the strongest tier the model-approval
  gate currently allows (see **The model ladder** below) before the sweep or the spawn ever reads
  it. There is no attempt cap any more — a bounce climbs the model ladder instead of counting
  toward a fixed number. A policy read that fails outright (the injected `read_policies` seam,
  `_backlog_load_policies`, raises) keeps the LAST
  good snapshot for that tick's refill if there is one, or skips refill entirely with no snapshot
  yet — either way the sweep (handoffs, give-ups, reconciliation) still runs, since stalling that
  on the same DB blip would be its own outage. The runner's OWN config schema keeps only
  fleet-wide knobs: `selection_strategy`, `max_concurrent`, `max_spawns_per_day`,
  `max_issues_per_pr`, `grouping_interval_minutes`.
  `retired_config_keys` (`eligible_label`, `enabled_repos`, `repo_weights`, `repo_caps`,
  `queue_min_status`, `max_attempts`, `default_model`, `respect_dependencies`, `needs_human_retry_days`)
  still lists the retired ones so a row that still carries them keeps the rest of its settings; on read, a stored
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
  model the session actually got and an `attempt_no` seeded from dispatch history
  (`server/pm/outcomes.py:prior_attempts` — the count of finalised, non-superseded `implement`
  outcomes already on the target — rather than a live claim counter, because every bounce now
  DELETES the claim row: the ladder's hold carries the item forward, not the claim, so a
  re-queued item has no `attempts` of its own to carry across a bounce). Each row also carries a
  `dispatch_kind` (`server/db/models.py:DISPATCH_KINDS`, `implement` | `investigate`; `NULL`
  predates the column and reads as `implement`) — only an `implement` row counts as a bounce or
  an attempt, so an `investigate` dispatch (series item 4b, never opens a PR by design) can never
  read as one that failed to. `BacklogRunnerLoop._sweep_one` finalises the row
  (`AutomationContext.finalize_outcome`) at whichever terminal branch closes the target: `merged`
  (carrying `first_pass_merge`), `needs_human` (a merged PR still needing a human close-out — the
  ladder plays no part in this path), `pr_gave_up` (PR Doctor gave up), `abandoned` (a person
  closed the PR unmerged, or the session never ran a single turn), or `no_pr` (the session ended
  with no mergeable PR — a bounce; the row still reads `no_pr` even when that bounce lands at the
  ladder's top and hands off to the owner, since the recorded OUTCOME was still "no PR", only the
  next step differs — see **The model ladder** below). A second dispatch over a still-`running`
  row (a double tap racing the loop) closes the earlier row `superseded` instead of `abandoned` —
  it never got to answer, so it is not counted as a bounce or an attempt. Both writers are
  best-effort (`server/pm/outcomes.py`, spec §13): a write failure is logged and never blocks a
  spawn or strands a claim mid-sweep — and a spawn-time write lost that way is recovered by the
  sweep, which passes the claim's own product/session/model/attempt (`_outcome_fallback`) as
  `finalize_outcome(fallback=…)` so the dispatch is inserted already finalised rather than
  forgotten, at most once per dispatch. The one surviving `upgrade_from=("no_pr",)` re-finalises a
  prior `no_pr` row in place, rather than inserting a second one, when a session later proves to
  have never run a turn on the same target — not an attempt-cap discovery; there is no attempt cap
  any more.

  **The model ladder (spec 2026-09-18 §4).** There is no fixed attempt cap. A bounce — a
  finalised, non-merged `implement` dispatch, whether a plain `no_pr` (session ended with no
  mergeable PR) or PR Doctor's `pr_gave_up` — climbs the NEXT dispatch one model tier above
  whatever the previous finalised attempt actually ran on (`server/pm/outcomes.py:next_rung`; an
  unresolvable previous model — `auto`, `default`, `best`, unknown — climbs from `sonnet`, so a
  first bounce under `auto` lands on `opus`), never below a pinned product `default_model`, and
  never above the product's `ladder_top` (`top_tier`; an enum over the model tiers defaulting to
  `fable` that replaced `max_attempts` in the policy schema — a stored policy still carrying the
  retired `max_attempts` key validates, the key is just dropped). A climb that would pass
  `ladder_top` is `None`: the bounce is AT the top, and it hands off to the human exactly like any
  other give-up (an `owner_action` hold, a comment, a `gave_up` row, and the same
  Retry/board-move/body-edit release signals — see the give-up paragraph below). A PR a PERSON
  closed unmerged is their decision, not a failed attempt: it hands straight to the owner with the
  PR link and never climbs the ladder at all.

  `give_up_after_bounces` is an optional **stricter** stop in front of that top, and it now
  defaults to `0` (never) — the ladder's own length is the cap. It has to be consulted BEFORE a
  rung hold is written, because the queue applies the same `bounced_out` predicate: a rung hold
  written at or past the dial is one no refill can ever consume, leaving the item `needs_spec`
  with no hand-off at all. So when this bounce reaches an operator's dial, `_sweep_one` routes
  through the same `_give_up` path a top-of-ladder bounce takes, with a reason naming the dial
  (`"bounced at opus; give_up_after_bounces=2 reached"`). Editing the issue body changes the
  fingerprint the count is keyed on and re-arms both halves together.

  Concretely, `_sweep_one` writes a `stronger_model` hold (via
  `SessionOrchestrator._backlog_escalate` → the shared `_backlog_write_runner_hold`,
  agent-settable, audited `pm.hold.set` with actor `runner`) BEFORE releasing the claim row —
  never after: a raise between the two leaves the claim intact for the next sweep to retry the
  whole branch, so the hold and the release can never land as a half-done pair. The SAME writer
  backs the existing give-up path (`_backlog_needs_human`, `kind="owner_action"`), so a person's
  hold is never overwritten by either, and an identical retry (same kind/actor/reason already in
  place) is a no-op, not a re-stamp. The `stronger_model` writer skips a held item outright — the
  "the runner may re-stamp its own hold" exemption is the give-up writer's alone, because an
  escalate that replaced an `owner_action` hand-off (even one the runner itself wrote) would put a
  handed-off issue back on the queue and drop its `gave_up` row, with no human signal anywhere. The hand-off reason names the tier the climb stopped on —
  `"bounced at {tier}; next attempt on {rung}"` (or `"PR Doctor gave up; next attempt on
  {rung}"`) when it climbs, `"bounced at {tier}: no mergeable PR after N attempts"` (or `"PR
  Doctor gave up at the top"`) at an UNCLAMPED top — and, when the top the climb stopped at was
  itself lowered by the model-approval gate (see below), appends which tier needs approval, e.g.
  `"bounced at opus; fable needs approval"`.

  On the next spawn, `SessionOrchestrator._backlog_ladder_rung` reads whether the primary issue
  still carries a `stronger_model` hold and, if so, composes the SAME `next_rung` inputs the
  sweep used to write it, then raises that rung to the capability floor (D9) — but never past the
  top, since a pinned rung above the top would park the dispatch behind a human instead of running
  it. The rung is PINNED as the dispatch's model (`config["default_model"] = rung`, recorded as
  `ladder_rung` on the spawned `AutomationEvent`'s detail) rather than fed through the `auto`
  assessor — no approval step for a ladder dispatch. The hold is released
  (`server/pm/holds.py:release_on_dispatch`, audited `pm.hold.release` with `signal="dispatch"`)
  only once the dispatch actually produced a NEW session that climbed — never for a spawn the
  context refused or de-duped onto an existing session, and never for one that ran at the ordinary
  pick because the hold read failed or `ladder_top` was lowered under the hold — so a re-run at
  the same tier still counts toward `give_up_after_bounces` with zero ladder progress.

  The effective `ladder_top` a product's policy carries is CLAMPED, every tick
  (`SessionOrchestrator._backlog_clamp_ladder_tops`), to the highest tier at or below the
  configured value that the model-approval policy currently allows (`resolve_model_policy`,
  `account=None`, judged app-wide): `create_session` would otherwise park a session dispatched
  onto a gated tier as a driverless `pending_approval` row, exactly what the clamp exists to
  prevent. Every tier is judged ONCE per policy load, not once per product. The two callers want
  opposite handling of a failed verdict read: the loader RAISES (it then keeps its previous
  clamped snapshot or skips refill — an unclamped snapshot is the unsafe direction), the queue
  read degrades to the last good verdicts instead. The configured value survives as an in-memory
  `ladder_top_configured` annotation (never persisted, never a `PRODUCT_POLICY_SCHEMA` field, read
  back through `gated_ladder_top`) so a hand-off at a clamped top can name the gated tier instead
  of silently stopping below the operator's setting.

  **Limits.** The ladder is Claude-only: a Codex bounce is still escalated onto the tier ladder
  (the same `stronger_model` hold, the same give-up at the top), but a Codex dispatch neither
  reads nor climbs a rung and never consumes the hold — a series follow-up. The rung is read from
  the batch's PRIMARY issue while the hold release clears the WHOLE batch. A per-account
  model-approval action stricter than the app-wide one judged at policy-load time can still park a
  ladder dispatch behind a human, because the account is not known until the spawn.

  **Evidence dispatch (series item 4b, spec 2026-09-18 §5).** An auto-filed error report is a
  stack trace, not a diagnosis: it must be investigated before anyone implements against it, so it
  is BORN parked. `SessionOrchestrator._pm_sync_upsert_items` writes an `evidence` hold
  (`server/pm/holds.py:set_evidence_hold`, actor `automation:error-triage`, reason
  `"auto-filed error report: investigate before implementing"`) the moment a NEW open issue
  carrying the error-triage label (`server/automations/error_triage.py:LABEL`) enters the mirror —
  only on the new-row branch, since re-holding an already-mirrored row on a later sync would undo
  the very investigation that cleared it. `evidence` is agent-settable and queue-eligible exactly
  like `stronger_model` (see the Holds paragraph in
  `docs/product/features/product-management-layer.md`), so the runner dispatches it as an
  ordinary candidate — but `server/pm/outcomes.py:dispatch_kind_for_hold` reads the batch's
  primary hold and answers `"investigate"` instead of `"implement"`, seeded onto the claim intent
  at CLAIM time (so a reconciler replay that never saw the spawn record still knows what the
  dispatch was for) and again at spawn, which is what picks
  `server/automations/backlog_prompt.py:build_investigate_prompt` over
  `build_implement_prompt`.

  The investigation prompt is read-only on code — open no PR, push nothing — and must end in
  exactly ONE of four exits: **close as noise** (comment + `gh issue close`), **consolidate**
  (comment "duplicate of #primary" + close on every sibling), **convert to a fix** (add a
  "## Findings" section to the primary issue's body, root cause and reproduction), or **escalate**
  (write what only the owner can supply into "## Findings"). Finishing is engine-neutral by
  construction: on Claude the session calls `mark_no_pr("not-needed", note=…)` (convert / close /
  consolidate) or `mark_no_pr("cancelled", note=…)` (escalate) — Claude alone has the durable
  signalling-tool surface; Codex, which has none, ends its final message with the same
  `NO_PR_MARKER` HTML comment every other Codex completion uses, phrased so
  `resolution_from_text` reads the two cases apart. The convert exit's note always starts
  `"converted to a fix: "` (`backlog_prompt.CONVERTED_NOTE_PREFIX`) — the one literal the prompt
  writes and the settle logic matches on. A session that ends without taking one of the four is
  the fifth, NO-exit case: the runner hands the issue to the owner exactly like an escalation, just
  with no note of its own.

  **Settle** (`BacklogRunnerLoop._settle_investigation`, only once the session has really ended —
  `_work_stopped` true AND `_session_alive` false; a session that never ran a turn is released for
  free like a no-PR bounce). An investigation opens no PR by design, so it is read from the issue's
  LIVE GitHub state and the hold, never from PR state:
  `server/automations/backlog_github.py:issue_state_live` (`gh issue view --json state`) and
  `server/sessions/orchestrator.py:_backlog_investigation_state` (the mirrored hold plus the
  session's own `sessions.pr_resolution_note`, migration `0052_pr_resolution_note`).
  - **Unknown issue state** (malformed ref, `gh` failure, an unparseable payload) — the row is left
    where it is and the next tick tries again. Never read as "still open". The retry is bounded:
    the consecutive-unknown streak is counted on the claim row
    (`investigation_unknown_ticks`, reset by the first readable answer) and logged at WARNING from
    the first one, and after `INVESTIGATION_UNKNOWN_TICKS_MAX` (12) failures in a row the claim is
    handed to the owner with the reason "could not read the issue state after 12 checks" and an
    `investigated` event whose `exit` is `unreadable`. Without that terminal a persistently 403ing
    App token held the row's `max_concurrent` slot forever, invisibly. The UNWIRED seam is exempt —
    it answers unknown by construction, not by failure, so a deployment that never wired it still
    settles nothing.
  - **Closed** — done: finalise the dispatch-outcome row `no_pr` and release the claim.
  - **Open, no longer mirrored** — nothing to hold and no way to tell which exit was taken; release
    the claim with no hold write.
  - **Open, and either the note starts with `CONVERTED_NOTE_PREFIX` or the `evidence` hold is
    already gone** (a session or a person cleared it directly through `set_hold`) — converted:
    clear the `evidence` hold FIRST (`server/pm/holds.py:release_on_investigation`, the
    `RELEASE_SIGNALS` signal `"investigation"`), then finalise the row, so the issue re-enters the
    queue as an ordinary implement candidate on the next refill.
  - **Open, held by something that is not the `evidence` hold this dispatch was for** — somebody
    else's decision (a person's `owner_action`, or any other kind) supersedes; release the claim
    and write nothing.
  - **Open, still `evidence`-held, no conversion note** — escalated or no-exit: hand off through
    the existing give-up path (`owner_action` hold + comment + `gave_up` row + the usual retry
    signals), carrying the session's own note as the reason — normalised through
    `issue_filing.neutralize_markers` (the same HTML-comment defence the error filer applies to
    quoted log text, so a note cannot smuggle a marker onto the public comment) and capped at
    `NOTE_REASON_MAX` (400 chars, ellipsis) by `_reason_from_note`; a note that collapses to
    nothing hands off as "investigation ended without an exit" rather than a bare "investigation: ".
    That one normalised string is also what the `investigated` event stores, so the copy rendered
    in the automation activity feed carries the same bound as the public comment.

  **Not a bounce.** Every investigate dispatch's `dispatch_outcomes` row carries
  `dispatch_kind = "investigate"`; `server/pm/outcomes.py:_implement_only()` — the one predicate
  shared by the bounce count and the ladder's `last_attempt_model` — only ever counts `NULL` or
  `"implement"` rows, so an investigation's by-design absence of a PR can never read as a failed
  implementation attempt or climb the model ladder.

  **The awaiting-decision ending (series item 5, spec 2026-09-18 §6).** A session that hits a
  genuine product question — which of two designs, which of three trade-offs — proposes options
  rather than guessing, and ends with `mark_no_pr("awaiting-decision")` (the third `mark_no_pr`
  resolution; Codex writes the same token through its `NO_PR_MARKER` comment). That ending is
  **neither a bounce nor a hand-off**, and `BacklogRunnerLoop._settle_decision` is deliberately the
  shortest branch in the sweep: finalise the `dispatch_outcomes` row as `no_pr` with cause
  `decision_proposed` (`DECISION_PROPOSED_CAUSE`), release the claim, post an activity event, and
  nothing else. No ladder climb — a stronger model cannot answer a question about what the product
  should do, and a `stronger_model` hold is queue-eligible, so it would re-dispatch the very item
  the runner just parked. No give-up — that would write an `owner_action` hold OVER the `decision`
  hold, destroying the link to the proposal the person must answer, and comment "needs human" on an
  issue whose inbox card already says so. And no bounce on the dial: `_counts_as_attempt` excludes
  the cause, so a dispatch that stopped to ask a question neither fails nor spends a rung.

  Releasing the claim is safe only because the item stays out of the queue on its OWN hold, so the
  settle verifies the HOLD, not the token. The token alone is a claim a session makes about itself,
  and two live paths produce it with nothing behind it: the marker heuristic reading a turn that
  proposed nothing, and a `propose_decision` that was REFUSED (bad options, a person's park, a
  proposal already pending) followed by the ending the model had already been told to use. An
  unheld `awaiting-decision` therefore falls through to the ordinary no-PR path — ladder, dial,
  hand-off — and logs `decision_resolution_without_hold`, rather than deleting the claim and
  handing the item back to the queue at the same tier with the ladder skipped, which with the
  shipped `give_up_after_bounces=0` nothing would terminate.

  **An investigation that proposes settles the same way.** Nothing withholds the `propose_decision`
  tool by dispatch kind, so an investigate dispatch can end on a decision too. `_settle_decision`
  runs AHEAD of `_settle_investigation` for exactly that reason: the investigate settle would
  otherwise read the `decision` hold as "somebody else parked it" and record
  `investigation: held by agent` — benign, but it makes "how often does an investigation end on a
  decision" unanswerable. It sits inside the investigate branch rather than above it because the
  two gates it needs (the session really ended, and it ran at all) live in each kind's own path.

  **The next run carries the answer.** Once a person accepts the proposal in the Inbox, the hold
  clears and the item is queue-eligible again. `Batch.decisions` (`ref` → the accepted decision
  text, from `server/pm/decisions.py:accepted_text_for_refs`) is threaded into the spawn, and
  `build_implement_prompt` renders a **"## Decisions already taken"** block quoting each accepted
  decision VERBATIM, prefixed with the ref it belongs to. Nothing re-words it — what the person
  picked is what the run implements. A batch with nothing decided produces a byte-identical prompt
  to the one before this landed.

  **Family grouping.** Sibling reports of ONE defect — one broken call site can raise from three
  frames and file three issues with three fingerprints, all sharing the same `logger:event`
  (`server/pm/error_family.py:family_key`, derived from the `ErrorSignature` rows at read time,
  never stored) — become ONE investigation instead of N sessions racing to close each other as
  duplicates. `server/automations/backlog_grouping.py:family_groups(candidates, max_k=…)` splits
  the candidate pool into evidence groups (bucketed by family in the pool's own queue-rank order,
  each capped at the product's `max_issues_per_pr` with overflow spilling into further groups of
  the same family right behind the first) and everything else; the evidence half never reaches the
  grouping model and is never mixed with implement candidates in one group — a single session
  cannot be handed both an investigate prompt and an implement prompt.
  `server/sessions/orchestrator.py:_backlog_attach_families` stamps `"family"` onto evidence
  candidates only (an implement candidate has no signature row to derive one from) and fails OPEN:
  a lookup that blows up leaves every evidence candidate familyless, which groups each one alone —
  the pre-family behaviour, never a lost tick. Sibling reports are bundled only up to
  `max_issues_per_pr`, which defaults to 1 — raise it to investigate a family in one session.

  **Plan order.** The two halves are interleaved, not concatenated: evidence groups are ranked
  among the other groups by their best member's queue rank
  (`backlog_grouping.order_by_pool_rank`, over the pool the global priority queue already ordered),
  and urgent work still leads. An investigation therefore competes for a dispatch slot exactly like
  any other candidate at its score — a `Ready`, high-score implement issue is not made to wait
  behind the day's auto-filed error reports, and an incident still goes first.

  **Still classified.** An `evidence` (or `stronger_model`) hold parks the WORK, not the FILING:
  `server/sessions/orchestrator.py:_classifiable_hold_clause` excludes only `HELD_KINDS`
  (`owner_action`, `decision`) from the classify-candidate set, so an auto-filed error report born
  under `evidence` is still classified normally — otherwise the entire auto-error stream would be
  permanently unclassified.

  **Limits.**
  - Sibling reports are bundled only up to `max_issues_per_pr`, which defaults to 1 — raise it to
    investigate a family in one session. At the default, every family bucket is size 1 and each
    auto-filed report is investigated alone.
  - The convert-to-a-fix exit is Claude-only in practice: the Codex driver/translator latch no
    resolution and no note (`pr_resolution_note` stays `NULL` for a Codex session), so a
    converting Codex investigation collapses to the hand-off exit instead — its findings are
    already in the issue body, which is the safe direction, but it is not read back as a
    conversion.
  - Already-mirrored auto-error issues get no birth hold: the write lives strictly on the new-row
    branch of `_pm_sync_upsert_items`, so there is no backfill for issues filed before this landed.
  - An unmirrored issue at settle time is released without writing a hold.
  - The issue state read at settle time is a LIVE GitHub call, never the lagging mirror; unknown
    (a `gh` failure, a malformed ref) retries next tick rather than being read as still open, for
    at most `INVESTIGATION_UNKNOWN_TICKS_MAX` (12) consecutive ticks before the claim is handed to
    the owner as unreadable.

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
  raised the pick, `"auto"` otherwise. A failed assessment means no floor either: with no tier to
  raise, the spawn drops to the account default rather than being pinned to the floor alone (the
  floor raises evidence over a guess, it never picks a model by itself). `POST /api/dispatch/assess`
  is deliberately unfloored — it carries only a free-form prompt, with no issue ref or capability
  whose history there would be to read. The spawned `AutomationEvent`'s `detail` carries
  `assessed`/`floor` only when set (so a pinned spawn's detail is unchanged from before this
  phase), and every `auto`-policy pick — raised or not — whose spawn produced a new session
  also writes one `backlog_runner.model_pick` audit row (identity `backlog-runner`, `{target, product, assessed,
  floor, applied, source}`), so "the assessor said sonnet but this capability's history floored it
  at opus" is recoverable after the fact. Neither the event detail nor the audit surfaces in the
  UI yet — `AutomationContext.list_events` drops `detail` entirely, so this is data for the
  history, not the activity feed, until a later phase reads it.

  `_backlog_query_queue` threads each ON product's own `give_up_after_bounces` into `rank`
  (`server/pm/queue.py`), which checks the `needs_spec` skip right after the `held` skip (the queue
  skips held items as `held`; the Backlog Runner's give-up writes the hold (`hold_by = runner`) and
  its `gave_up` row survives only until the hold is gone) and before the board-status gate: a row
  whose body (by `server/pm/outcomes.py:body_fingerprint`)
  has bounced `give_up_after_bounces` times or more (`bounced_out`) is skipped `needs_spec`
  rather than ranked — the SAME predicate the roadmap's leaf chip reads (see
  `docs/product/features/product-management-layer.md` § Dispatch outcomes). The status card's
  per-product segment appends ` · N bounced` when `N` is nonzero (`BacklogRunner.status`), and the
  SAME per-product rows also ride structured on `AutomationStatus.products` (a new field beside
  `text`, never a sibling top-level key — `GET /api/automations` serializes it once, inside each
  row's `status`): `[]` when the runner has products but none has dispatch on, `None` for an
  automation with no concept of products (PR Doctor) or while the read has failed. The runner's
  detail pane renders it as a table on the Queue tab (`docs/PAGE-TABS.md`) whenever it is a non-empty array
  (`AutomationDetail.tsx`, via the shared `CheckTable` primitive): Product (linked to
  `/roadmap/:slug`), Dispatch, In flight (`inFlight/cap`), Eligible, Bounced.

  The top pick is re-verified against live GitHub immediately before the
  claim — that check **fails closed** (§10), so a mirror lagging reality costs a tick, never
  a session spawned on stale work. The old `eligible_label` gate is retired: board status,
  not a label, says what is workable.
  **The propose-mode tap re-verifies too** (`BacklogRunnerLoop.live_check_ok`, #1523). A
  `proposed` claim was verified when the proposal was *made*, which can be hours before a person
  taps Implement, so the tap runs the same gate — one fail-closed rule, one cooldown ledger, one
  `skipped_live_check` event distinguishing `not_ready` (GitHub says no) from `check_failed`
  (GitHub could not be asked). A tap that does not pass returns `not_ready` and **keeps its
  claim**: the refusal is about this attempt, not the work, and releasing would drop the batch
  back into a pool whose next pick hits the same blocker. `backlog_runner_dispatch` is an
  explicit operator override and stays ungated — an operator must be able to dispatch work the
  mirror or GitHub is currently wrong about.
  Grouping is a background Sonnet optimizer (#610) persisted in `backlog_groups`; selection
  ordering supports balanced / priority / weighted / concurrency-cap strategies (#773) for
  per-repo fairness (#771) — `priority` now serves the ON products' repos oldest-first by
  `Project.created_at`, the deterministic stand-in for the retired hand-ordered `enabled_repos`
  list. **Conflicting work shares one durable ownership store** (`ownership.py`, #1611).
  Canonical GitHub node IDs identify every issue in a batch and every PR plus its verified
  linked issues. Automatic and manual dispatch, PR repair, consolidation, and map branch
  updates acquire their complete target set together. Repository aliases and overlapping
  batches cannot create separate owners for the same work. Target-state rows retain in-flight
  status and outcomes; every bounce now deletes the row (its `attempt_no` provenance lives on
  the dispatch-history rows instead, see **Dispatch outcomes** below), so a claim's own history
  never outlives the bounce that ends it. An issue comment remains a human-visible trace, never
  ownership.

  Launches reserve a session UUID before external work. A repeated request recovers that
  session or reports uncertainty. A missing driver, expired timer, or completed status alone
  cannot release ownership: managed native work and known children need stopped evidence,
  and later execution must pass the same ownership check. Unresolved legacy work also blocks
  conflicting dispatch. This guarantee covers ViberPit-managed work, not arbitrary external
  actors or detached commands outside its managed execution boundary.

  Pending proposals and no-account waits remain discoverable as complete batches in the
  action list. GitHub accepting a branch update is distinct from its completion; observation
  must establish the requested update before releasing its targets. Failed or partial issue
  closes retain ownership and a review reason. Recovery observes completed effects without
  automatically replaying an uncertain mutation. Mixed batches with intentionally open
  members can still require operator review after an uncertain close; broader recovery
  controls belong to the later recovery phase of #1611.
  A **give-up is the same row marked `gave_up`**, which replaces the claim (a target waiting on a
  human holds no slot); the reconciler turns it into board Status `Held`. The queue skips
  held items as `held`; the Backlog Runner's give-up writes the hold (`hold_by = runner`) and its
  `gave_up` row survives only until the hold is gone (spec 2026-09-18 §7, `server/pm/holds.py`).
  Because the row and the hold are two transactions, the row carries `held`: false until the hold
  write returns, and a row that is still `held: false` is never dropped — dropping it would
  re-dispatch the issue with no handoff at all. Such a row is re-announced every tick until the
  hold write lands, with two exceptions: if the item is already held the row adopts that hold
  instead (re-announcing would re-stamp `hold_set_at`/`hold_fingerprint` and lose a pending
  body-edit release), and a pre-ownership row (#1648, no `ownership_token`) is left inert, since
  it can never record `held` and would otherwise re-announce forever.
  A human takes an issue back from a give-up either by one of the hold-release signals —
  moving the board card to Ready or Backlog, editing the issue body (the fingerprint no longer
  matches), or the Retry tap (`POST /api/pm/work-items/{id}/hold/release`) — or by **dispatching
  it manually** (`POST /api/automations/backlog-runner/dispatch`, or the card's dispatch action),
  which requests a fresh claim after prior ownership is safely reconciled; moving the board card
  now DOES release the hold (#1540). Backoff + disk-exhaustion circuit breaker in #626/#683.
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
- Per-product automation policy (phase 1 — scope, reach, caps/weights/ladder/model, the
  one-shot seed off the retired repo knobs): `server/automations/product_policy.py`,
  `server/sessions/orchestrator.py` (`_backlog_query_queue`, `_backlog_select_batch`,
  `_backlog_seed_policies`), `web/src/features/roadmap/detail/AutomationPolicyCard.tsx`;
  see `docs/product/features/product-management-layer.md` § Product automation policy and
  `docs/superpowers/specs/2026-09-12-product-automation-policy-design.md`.
- Claim-label retirement (#994): one-time sweep, completed 2026-09-11; the code was removed
  afterward — see `specs/003-gh-projects-integration/phase-d-runbook.md` for the record.
- Action surface / consolidation / API / web: `server/automations/action_providers.py`,
  `server/automations/consolidation_engine.py`, `server/api/automations.py`,
  `web/src/features/delivery/automation/AutomationDetail.tsx`,
  `web/src/features/delivery/automation/actions.tsx`.
- PRs: #610 (precomputed grouping optimizer), #618 (manual issue dispatch), #626 (spawn
  backoff + disk circuit breaker), #645 (contextual nudges, two-tier decider), #575 (human
  deference), #640 (armed-PR requirement for `--squash --auto`), #683 (per-target backoff
  retune), #786 (don't auto-close action-required / docs-only issues, fixes #784), #550
  (per-automation session model), #469 (never auto-archive a manual unarchive).
