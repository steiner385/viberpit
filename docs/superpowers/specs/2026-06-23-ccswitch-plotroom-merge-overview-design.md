# ccswitch ⨉ Plotroom Merge — Program Overview

**Date:** 2026-06-23
**Status:** Approved (overview); Phase 0 specced separately
**Scope:** This is a **migration program**, not a single feature. It is decomposed into
phases, each of which gets its own spec → plan → build cycle. This doc is the roadmap and
the shared rationale; it is intentionally not implementable on its own.

## Goal

Fold **Plotroom** (npm `plotroom`, formerly `pr-dashboard` — a Node/Express + React
PR/CI pipeline dashboard) into **ccswitch** (the Python/FastAPI + React multi-account
Claude session manager) so they become **one app with one codebase, one UI, and one
GitHub poller**, then build three automations on top of the unified app that close the
loop between *running sessions* and *the PRs those sessions ship*.

## Why (the loop we're closing)

- **ccswitch** knows *what the agents are doing right now*: per-session status
  (idle/done/needs-input/crashed), the worktree branch each session owns, and account
  rotation.
- **Plotroom** knows *what happened to the work the agents shipped*: each open PR's
  pipeline stage (CI → merge-queue → QA → prod), learned ETAs, merge-queue stalls, and
  ship/break events.
- Today nothing connects them. A session finishes and opens a PR; the PR lands or breaks;
  no signal flows back to the session or the operator. **The merge makes session state
  and pipeline state queryable in one place, which is the precondition for the three
  automations.**

## Decision record (from brainstorming, 2026-06-23)

1. **Biggest pains:** kicking off / feeding work, triage ("what needs me"), and post-PR
   follow-through. (Account/usage juggling was *not* selected.)
2. **Autonomy policy:** **mixed by risk** — read/observe flows act automatically; any
   *write* (starting a session, pushing a fix, merging) only *proposes* and waits for
   approval. This is a property of read-vs-write, not a per-feature toggle.
3. **Work sources** for an idle session: open GitHub issues, Plotroom-flagged
   broken/blocked PRs, and PR review follow-ups. (A hand-curated backlog was *not*
   selected — all sources are GitHub-derived.)
4. **Surfaces** for proposals/alerts: the **ccswitch dashboard** (suggestion chips on
   session cards) and **push/phone** (for away-from-desk urgency). (Folding into
   Plotroom's UI and a daily digest were *not* selected.)
5. **Consolidation level: Level 3 — true single-process merge (rewrite).** Considered and
   rejected: Level 1 (loose event bridge — stays two apps, two pollers) and Level 2
   (embed Plotroom's component + run its Node backend as a supervised sidecar — one app
   to the user, no rewrite). The user chose Level 3 deliberately: one codebase to
   maintain, no permanent cross-runtime plumbing, and an agent fleet that makes a large
   mechanical port tractable.

### Why the merge target is ccswitch (Python), not Plotroom (Node)

The `claude-agent-sdk` that runs the sessions is a **Python** library and is the
irreducible core of ccswitch — it cannot move into a Node process. Plotroom's engine is
"just" data processing over the GitHub API and SQLite, so it *can* be re-expressed in
Python. Therefore the only viable direction is **port Plotroom's Node engine into
ccswitch's Python backend** and **merge Plotroom's React UI into ccswitch's React app**.

### The two halves are very unequal

- **Frontend merge — the easy half.** Both apps are React + Vite + TS + Tailwind.
  Plotroom's `<PrDashboard/>`, status strip, metro-map rows, and check Gantt become a
  **Pipeline view** inside ccswitch's existing web app. Mostly port-and-rewire.
- **Backend port — the hard half, and the risk.** Re-implementing in Python:
  - `server/pipeline-model/` — the stage machine (`parse-jobs`, `parse-triggers`,
    `expand-matrix`, `gating`, `narrow-events`, `derive-static`).
  - `server/estimator/` — the ETA engine (`progress` conditional-remaining estimator,
    `waits` runner pickup-wait learning, `regression`, `starvation`, `queue-health`,
    `deploy-status`, `critical-path`, `calibrate`, `flake-summary`, …).
  - `server/ancestry.ts` — deploy ancestry over the GitHub compare API.
  - `server/history.ts` — the ~15-table learned-history schema (`check_durations`,
    `queue_waits`, `runner_waits`, `eta_accuracy`, `group_runs`, `observed_pools`, …).
  - `server/notifier.ts` + `server/digest.ts` — the notification taxonomy, debounce, and
    daily digest.
  - SSE live stream + frame dedup; the cost explorer + actuals import.

## Single-source-of-truth obligations (constitution Principle VI)

This program *exists to remove* a parallel implementation, so it must not create new ones:

- ccswitch already ships `server/github/pr_status.py` + `pr_poller.py` (a `PrStatusPoller`
  mirroring `UsagePoller`, keyed on the worktree branch ↔ PR `headRefName` join). The port
  **absorbs and replaces** this — it is not left running alongside the new engine.
- The worktree branch ↔ session join already exists (`Session.branch`, set from
  `WorktreeInfo.branch`). It must be read **live** from the worktree HEAD
  (`worktrees.live_branch`), never the stale stored creation branch (a known trap: the
  completion workflow moves the worktree onto a fresh PR branch).
- During the port, the new Python engine and the live Plotroom instance run **in
  parallel** only as long as the **parity harness** (Phase 0) needs them both as oracle
  vs. candidate. Once a capability reaches parity and cuts over, the corresponding
  Plotroom path is decommissioned in the **same phase** — no half-dead duplicate.

## Phase decomposition (strangler-fig)

Each phase is its own spec. Cut over a capability only once the parity harness shows the
Python output matches the live Plotroom `GET /api/state` (and `/api/metrics`).

- **Phase 0 — Foundation + parity harness.** Stand up the Python PR/CI data model and
  GitHub polling (absorbing `PrStatusPoller`), and build a harness that diffs ccswitch's
  computed state against the live Plotroom `/api/state` for the same repos. De-risks every
  later phase. **(Specced in `2026-06-23-plotroom-merge-phase0-parity-foundation-design.md`.)**
- **Phase 1 — Pipeline stage machine + ETA engine.** Port `pipeline-model/` and
  `estimator/`; parity-test each estimator file against observed history.
- **Phase 2 — Deploy ancestry + SSE live stream + notification taxonomy/digest.**
- **Phase 3 — Frontend merge.** Plotroom's Pipeline UI into ccswitch's React app; retire
  the standalone Plotroom UI.
- **Phase 4 — Cost explorer + remaining config surfaces; decommission Plotroom** as a
  separate service entirely.
- **Phase 5 — Triage Lens** *(read-only → automatic)*: a ranked attention queue across
  all sessions, fusing session status with the now-native pipeline state; push only for
  the top tier (a session waiting on you, or a crash).
- **Phase 6 — Work Dispatcher** *(write → propose)*: when a session goes idle/done, a
  suggestion chip proposes the next task from the three sources (top issue via
  `prioritize-issues`/`implement-loop`; a flagged broken PR via `gh-fix`; a review
  follow-up via `pr-review`). Approve → launch the session pre-loaded with the skill.
- **Phase 7 — PR Follow-through** *(mixed)*: pipeline events route back to the owning
  session by live branch — badge/triage updates automatic, push on break/stall/ship,
  and a fix proposal chip on break. This was the user's most-wanted automation; it lands
  last because it composes Phases 5–6.

## The three automations share two primitives

Built once in the early automation phases, reused by all three:

1. **Live branch → session map** (`worktrees.live_branch`), and
2. **The proposal-chip surface** on session cards (the existing detached done/idle
   suggestion mechanism — `latest_suggestions` + ghost text + Resume/chevron — is the
   foundation to extend, not to reimplement).

## Risks

- **Behavioral drift** porting tuned heuristics (the conditional-remaining estimator,
  pickup-wait learning, the phase-aware `needs:` graph). Mitigation: the Phase 0 parity
  harness is a hard gate on every later cutover.
- **History continuity.** Plotroom's learned history (`history.db`) is what makes ETAs
  accurate from day one. The port must migrate or re-backfill this, not cold-start it.
- **Scope creep into a big-bang rewrite.** Mitigation: strict strangler phasing; each
  phase ships a working unified app with one capability cut over and its Plotroom
  counterpart removed.
- **Two test/lint toolchains during transition** (pytest/ruff/mypy vs vitest/tsc/eslint).
  Accepted; converges as phases land.

## Out of scope (for the whole program, unless a later phase promotes it)

- Plotroom's GitHub App mode and webhook receiver as *new* ccswitch features (port only
  if a phase needs them; polling is the primary mechanism).
- Kiosk mode (low value inside the session manager).
- Auto-acting write flows — every write stays a proposal per the autonomy policy until the
  user explicitly promotes a specific flow to auto.
