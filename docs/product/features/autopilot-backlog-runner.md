---
slug: autopilot-backlog-runner
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
    - server/automations/action_providers.py
    - server/automations/consolidation_engine.py
    - server/automations/completion_bar.py
    - server/api/automations.py
    - web/src/automations/AutomationsTab.tsx
  prs: [610, 618, 626, 645, 575, 640, 683, 786, 550, 469]
---

# Autopilot & Backlog Runner

**Problem** — Running autonomous development on ccswitch means two distinct, tedious jobs
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
  what the operator sees is what agents do next. It is scoped by `enabled_repos` (empty =
  every onboarded product *for candidate selection only* — the `priority` selection
  strategy reads the list directly, so an empty list makes that strategy a
  no-op), gated by `queue_min_status` (`Ready`, the safe default, or
  `Backlog` to reach untriaged items), dependency-gated from the compiled graph
  (`respect_dependencies`; native blocked-by edges, `Depends on #N` body references and epic
  list order), and the top pick is re-verified against live GitHub immediately before the
  claim — that check **fails closed** (§10), so a mirror lagging reality costs a tick, never
  a session spawned on stale work. The old `eligible_label` gate is retired: board status,
  not a label, says what is workable.
  Grouping is a background Sonnet optimizer (#610) persisted in `backlog_groups`; selection
  ordering supports balanced / priority / weighted / concurrency-cap strategies (#773) for
  per-repo fairness (#771). A **claim is the runner's durable target-state row** (#994): writing
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
  cooldown (follow-up #FOLLOWUP). Backoff + disk-exhaustion circuit breaker in #626/#683.
- The runner had to learn to see its own PRs (#546 sweep-time resolution) and to fail-closed
  on PR↔issue linkage before closing (#643), and not to auto-close action-required /
  docs-only issues (#784/#786).
- Consolidation (`consolidation_engine.py`) clusters same-scope open PRs into low-risk merge
  candidates for a human-driven consolidation session; PRs already being consolidated are
  flagged and linked (#555). Manual actions for all automations are enumerated by
  `action_providers.py` and driven through `server/api/automations.py`; the UI is the
  top-level Automations tab (#578) with per-automation session model selection (#550).

**Evidence** —
- Autopilot core / decision: `server/automations/autopilot.py`,
  `server/automations/autopilot_decider.py`, `server/automations/autopilot_policy.py`,
  `server/automations/completion_bar.py`.
- Backlog work selection / claim / spawn: `server/automations/backlog_runner.py`,
  `server/automations/backlog_selection.py`, `server/automations/backlog_grouping.py`,
  `server/automations/backlog_github.py`.
- Claim-label retirement (#994): `server/pm/label_retirement.py`,
  `scripts/retire_claim_labels.py`, `specs/003-gh-projects-integration/phase-d-runbook.md`.
- Action surface / consolidation / API / web: `server/automations/action_providers.py`,
  `server/automations/consolidation_engine.py`, `server/api/automations.py`,
  `web/src/automations/AutomationsTab.tsx`.
- PRs: #610 (precomputed grouping optimizer), #618 (manual issue dispatch), #626 (spawn
  backoff + disk circuit breaker), #645 (contextual nudges, two-tier decider), #575 (human
  deference), #640 (armed-PR requirement for `--squash --auto`), #683 (per-target backoff
  retune), #786 (don't auto-close action-required / docs-only issues, fixes #784), #550
  (per-automation session model), #469 (never auto-archive a manual unarchive).
