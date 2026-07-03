# Autopilot — Continuous Session Advancement — Design

**Date:** 2026-07-02
**Feature area:** 001-ccswitch-web (web session manager)
**Status:** Approved design, pending implementation plan
**Builds on:**
- `2026-06-30-backlog-runner-design.md` — the control-loop / injected-seam / circuit-breaker
  pattern this feature mirrors (`server/automations/backlog_runner.py`).
- `2026-06-30-pr-doctor-design.md` — sibling automation; the `off/propose/auto` mode and
  `needs_human` handoff conventions.
- `2026-06-22-goal-conformance-and-indicators-design.md` — the existing `active_goal`
  (`/goal`) field autopilot reuses as the highest-authority goal signal.
- `2026-07-02-inferred-session-goals-design.md` — **companion feature (optional dependency).**
  Adds the LLM-inferred middle tier to `effective_goal()`. Autopilot ships and works without
  it (degrading to explicit-goal → initial-prompt); when it lands, completion judgement gets
  sharper for sessions with no explicit `/goal`.

## Problem

ccswitch runs many live `claude` sessions across accounts. Left alone, sessions **stall**:
a turn finishes (`done`/`idle`) with work half-complete, a tool-permission prompt sits
unanswered (`waiting_input`), an `AskUserQuestion`/`ExitPlanMode` blocks (`waiting_question`/
`ready_review`), or a turn fails (`needs_attention`). Each stall requires a human to notice
and nudge. The user wants an **autopilot** that "constantly keeps all sessions moving until
complete," making the *right* decisions per the project's coding habits and standards.

The backlog runner *starts* work (spawns sessions from issues). Nothing *advances* work
already in flight. Autopilot is the missing sibling: a supervisor that resolves each stuck
state correctly and drives every governed session toward its goal.

## Goals

- A global automation (`off`/`propose`/`auto`) that keeps every **governed** session out of
  stuck states, acting through existing orchestrator seams — no new orchestration primitives.
- Correct decisions at four stuck points: **nudge** `done`/`idle`, **approve/deny** tool
  permissions, **answer** questions / approve plans, **recover** `needs_attention`/failures.
- Decisions gated by **hard-rule guardrails** (safety-critical, code-defined, non-negotiable)
  with an **LLM decider** for judgment calls, informed by a versioned guidance doc.
- A per-session **opt-out** so any session can be pinned to manual.
- "Complete" = the session's **effective goal** is met, judged against that goal — not a fixed
  workflow milestone. The goal is resolved through a single `effective_goal()` helper (explicit
  `/goal`, else the initial prompt; a companion feature adds an LLM-inferred middle tier).
- Restart-safe, non-reentrant, and fully testable with **no network, no real model, no real
  sessions** (all I/O seams injected, mirroring `BacklogRunnerLoop`).

## Non-goals (v1, YAGNI)

- Autopilot does **not** merge PRs or drive CI (that is PR Doctor's territory); it drives a
  session to the point where its goal is met and, per repo norms, a PR is opened + auto-merge
  armed. Handoff to PR Doctor for the merge tail, exactly as the backlog runner does.
- No bespoke frontend for the global control — the generic `AutomationView`/`AutomationCard`
  renders it from `config_schema` + `modes` + `status()`.
- No Channel-B text parsing of `/goal met` (the goal-conformance spec's deferred boundary).
- No cross-session planning / dependency ordering; autopilot treats sessions independently.
- No raw-terminal interception; autopilot acts only through the structured orchestrator seams.

## Architecture overview

A new automation `autopilot`, a sibling of the backlog runner. New files:

| File | Purpose |
|------|---------|
| `server/automations/autopilot.py` | `Autopilot` (metadata + `config_schema` + `status()`, registered in `registry.py`) and `AutopilotLoop` (non-reentrant control loop, injected seams). |
| `server/automations/autopilot_policy.py` | **Pure, synchronous hard-rule guardrails.** No I/O. The safety-critical core; trivially unit-testable. |
| `server/automations/autopilot_decider.py` | The LLM decider — one-shot Haiku query mirroring `rank_issues` (injected `_query_fn`, structured output, safe default on any failure). |
| `server/automations/autopilot_guidance.md` | Versioned decision-heuristics doc loaded into the decider's system prompt. |

Reused (no new versions — constitution VI):
- `AutomationContext` — activity `emit`, durable per-session ledger (target-state), config read.
- `Automation` DB row (`enabled`/`mode`/`config`) + `registry.ensure_rows()` + generic API/UI.
- Orchestrator action seams: `ensure_driver`, `resolve_permission`, `answer_question`,
  plan-approval, `send_prompt` — all invoked under `SYSTEM_IDENTITY`.
- `set_goal_for_session`/`_persist_goal` persistence path for goal writes.

## Control loop (`AutopilotLoop.tick()`)

Driven by `_autopilot_tick_loop(interval=20.0)` **and** nudged from the orchestrator's
`_persist_status` callback whenever a governed session enters an actionable state (mirroring
the backlog runner's end-of-session nudge at `orchestrator.py:752`). The interval is the
safety net; the nudge makes it feel instant. `tick()` runs the whole body under a single
`asyncio.Lock` — an interval tick and a status nudge must never overlap (a session could
otherwise be double-acted). Governed-session state is always re-derived from `orch._records`
inside the lock, never cached across ticks.

Each `tick()`:

1. `read_config()` → `(mode, config)`. If `mode == "off"` **or** `orch._draining` → return
   (autopilot must never fight a deploy drain).
2. `list_governed_sessions()` → non-terminal sessions that are **not** opted-out
   (`autopilot_paused`), are in an actionable status
   (`done`, `idle`, `waiting_input`, `waiting_question`, `ready_review`, `needs_attention`),
   and — when `enabled_repos` is non-empty — belong to one of those repos.
3. For each governed session (per-session `try/except` — one session's failure never stalls
   the tick, exactly like `_sweep_one`):
   1. `get_decision_context(session_id)` — assembles the **DecisionContext** (see below).
   2. **Guardrails (pre-filter)** — `autopilot_policy.pre_decide(ctx, ledger, config)`.
      A hard `deny`/`skip`/`escalate` verdict short-circuits and the decider is never called.
   3. If unresolved → **LLM decider** — `autopilot_decider.decide(ctx, guidance)` returns a
      validated `Decision`.
   4. **Guardrails (post-validate)** — `autopilot_policy.post_validate(decision, ctx, config)`.
      The decider may never upgrade a rule-denied tool to `approve`; an invalid decision is
      downgraded to `escalate`.
   5. **Apply** (`auto`) or **propose** (`propose`): `ensure_driver` if detached, then the
      matching action seam. Record the action + stuck-state fingerprint in the ledger.

`propose` mode performs steps 1–4 and then `emit`s the decision as a proposal instead of
mutating (a human one-taps to apply), matching the backlog runner's propose semantics.

### DecisionContext (assembled by `get_decision_context`)

A plain dict/dataclass, no live objects, so the decider and guardrails are pure:

```
{
  session_id, status, account, repo, branch,
  effective_goal: str,            # see "Effective goal" below
  goal_source: "explicit"|"inferred"|"initial_prompt",
  stuck: {
    kind: "permission"|"question"|"plan"|"done"|"needs_attention",
    # permission:
    tool_name, tool_input,              # e.g. "Bash", {"command": "git push --force"}
    # question:
    question, options: [str, ...],
    # plan:
    plan_text,
  },
  transcript_tail: [ {role, text|tool}, ... ],   # last ~N events, truncated
  ledger: { actions_this_hour, last_fingerprint, repeat_count, ... },
}
```

### Decision (returned by the decider, validated)

```
{
  action: "approve"|"deny"|"answer"|"nudge"|"complete"|"escalate",
  answer_index: int | null,     # for "answer" (question) / plan approve
  prompt: str | null,           # for "nudge"/recover
  reason: str,                  # always present; shown in the activity feed
}
```

Applied via: `approve/deny` → `resolve_permission`; `answer` → `answer_question`
(plans are answered the same way the UI approves an `ExitPlanMode`); `nudge`/recover →
`send_prompt`; `complete` → `emit` "goal met", then in `auto` mode **archive the
session** (reversible; via a dedicated `archive` seam — never through `apply`) and
delete its ledger row; in `propose` mode only propose the archive;
`escalate` → `emit` + mark `needs_human` in the ledger (no mutation).

## Decision engine — GUARDRAILS (the centerpiece)

Guardrails are **pure functions in `autopilot_policy.py`** with no I/O, so every rule is a
direct unit test. They run in two passes around the LLM decider. **A guardrail verdict always
wins over the decider** — the model can only choose *within* what the rules already permit.

### Precedence (highest first)

1. **Drain & lifecycle** — if `orch._draining`, or the session is terminal/opted-out, the
   session is never acted on (enforced in `list_governed_sessions`, restated here defensively).
2. **Destructive-action deny-list (tool permissions).** A permission whose `tool_name` +
   `tool_input` matches any pattern below is **always denied**, regardless of the decider:
   - `git push --force` / `push -f` **without** `--force-with-lease` (repo Git-safety norm).
   - History rewrite on a shared ref: `git reset --hard` onto `origin/*`, `git rebase` /
     `filter-branch` / `push --mirror` targeting a remote branch.
   - Bulk/destructive filesystem ops: `rm -rf` on a non-scratch path, `git clean -fdx`,
     truncating/deleting files the session did **not** create in this run.
   - Secret exfiltration shapes: reading `~/.ssh`, `.env`/credentials being piped to network
     (`curl`/`nc`/`| sh` from a fetched URL).
   - Any `gh`/`git` operation that **merges or deletes** a branch/PR (autopilot advances, it
     does not merge — PR Doctor owns the merge tail).
   The baseline list is **hard-coded**; `extra_deny_patterns` config can only **extend** it,
   never shrink it. A denied permission emits a `guardrail_denied` event with the matched rule.
3. **Approval master-switch.** If `allow_tool_approvals` is `false`, autopilot **never**
   approves any permission — it may only `deny`, `answer`, `nudge`, or `escalate`. (Lets an
   operator run autopilot in a "moves work but never grants tools" posture.)
4. **Human deference.** If a human interacted with the session within `human_defer_seconds`
   (derived from `last_turn_at` / identity threading), **skip** — don't race a person mid-task.
5. **Per-session hourly budget.** If the ledger shows `actions_this_hour >=
   max_actions_per_session_per_hour`, **skip + escalate once** (rolling window, like the
   backlog circuit-breaker). Prevents a runaway session from consuming the loop.
6. **Global daily cap.** `max_actions_per_day` across all sessions (rolling 24h over an
   injected clock) — a global circuit-breaker. Tracked on the **loop instance** (an in-memory
   list of action timestamps pruned to the rolling day, exactly like the backlog runner's
   `_spawns_today`), **not** in any per-session ledger. When open, emit `circuit_open` once and
   skip new actions (escalations still run).
7. **Loop detection.** The stuck-state **fingerprint** = a stable hash of
   `(kind, tool_name|question|plan_head, effective_goal)`. If the same fingerprint recurs
   `loop_repeat_limit` times for a session, autopilot **stops and marks `needs_human`** — a
   nudge that isn't landing must not be re-sent forever.
8. **Recovery attempt cap.** For `needs_attention`, at most `recovery_max_attempts` recovery
   nudges before `needs_human` (mirrors the backlog runner's `max_attempts`).

### Post-validate rules

- A `Decision.action == "approve"` is downgraded to `escalate` if the permission matches the
  deny-list or `allow_tool_approvals` is off (defense in depth — the decider is untrusted).
- `answer_index` must index a real option; out-of-range → `escalate`.
- An empty/whitespace `nudge` prompt → `escalate`.
- Any decider parse/model failure already returns `escalate` from the decider; post-validate
  is the second net.

**Default posture:** every ambiguous or failed path resolves to **`escalate`** (leave it for
a human), never to a mutation. Safety is the default, action is the exception.

### LLM decider (`autopilot_decider.py`)

One-shot Haiku query, structurally identical to `rank_issues`:
- **System prompt** = autopilot's role + the full text of `autopilot_guidance.md`.
- **User payload** = the `DecisionContext` (stuck payload + transcript tail + effective goal).
- **Output** = the `Decision` JSON, parsed defensively; unknown/again-malformed → `escalate`.
- `model="haiku"`, `allowed_tools=[]`, `setting_sources=[]`, injected `CLAUDE_CONFIG_DIR`/`cwd`,
  injected `_query_fn` so **the real model is never called in tests**.

### `autopilot_guidance.md` (decision heuristics — NOT a copy of CLAUDE.md)

Versioned in-repo so the standards are diff-able, code-reviewed, and cannot be silently
changed at runtime. It encodes *how autopilot decides*, distinct from coding style:
- **Approvals:** allow safe reads/edits/tests/formatters/`git add`/commit/push-with-lease/PR
  creation; defer anything touching prod/deploy/secrets/history to a human (`escalate`).
- **Answers/plans:** pick the option that best serves the effective goal and the repo's norms
  (TDD, no parallel implementations, purge replaced code, `Fixes #N` in PR bodies); when a
  plan proposes broad refactoring beyond the goal, prefer the narrower option or `escalate`.
- **Nudges:** drive toward the CLAUDE.md completion workflow — tests+lint green → commit →
  push → PR ready → `gh pr merge --squash --auto` — then treat the goal as met.
- **Completion:** judge the effective goal *actually met* (PR open with auto-merge armed for a
  code task), not merely "the turn ended."
It explicitly references (does not duplicate) `CLAUDE.md` and the constitution as the source
of truth for the standards themselves.

## Effective goal

Autopilot needs a goal to judge completion. There must be **one** answer to "what is this
session's goal," so autopilot introduces a single resolver helper (any parallel goal lookup is
the anti-pattern to avoid). In this feature it has **two tiers**:

```
def effective_goal(record) -> tuple[str, str]:   # (goal_text, source)
    if record.active_goal:  return record.active_goal, "explicit"          # user /goal — wins
    return record.initial_prompt_summary, "initial_prompt"                  # fallback
```

- **`active_goal`** — existing field; the built-in `/goal` condition (explicit human intent),
  highest authority. Reused as-is; not duplicated.
- **fallback** — a summary of the session's initial prompt when no `/goal` is set.

The companion feature `2026-07-02-inferred-session-goals-design.md` **inserts a middle
`"inferred"` tier into this same helper** (an LLM-derived `inferred_goal`) — it edits
`effective_goal()` in place rather than adding a second resolver. Autopilot is written against
the helper's *contract* (`(text, source)`), so it needs no change when that tier appears;
sessions without an explicit `/goal` simply get a sharper completion judgement.

## Durable state & loop prevention

Per-session ledger via `AutomationContext` target-state, `target == session_id`:

```
{ hour_actions: [float, ...],   # timestamps, pruned to the rolling hour
  last_fingerprint: str, repeat_count: int, recovery_attempts: int,
  escalated: bool }
```

Restart-safe, bounded (deleted when the session goes terminal, like the backlog runner deletes
its target-state on merge/give-up). Backs the **per-session hourly** budget, loop detection,
and `needs_human` escalation. (The **global daily** cap lives on the loop instance, not here —
see guardrail 6.) All ledger I/O is best-effort — a read/write failure degrades to
"skip this session this tick," never a crash.

## Config schema (operational knobs only)

Rendered generically by `AutomationCard`. `modes = ("off", "propose", "auto")`.

| key | type | default | meaning |
|-----|------|---------|---------|
| `allow_tool_approvals` | bool | `true` | If off, autopilot never approves permissions. |
| `max_actions_per_session_per_hour` | int | `12` | Per-session rolling circuit-breaker. |
| `max_actions_per_day` | int | `300` | Global rolling 24h circuit-breaker. |
| `recovery_max_attempts` | int | `2` | Recovery nudges before `needs_human`. |
| `loop_repeat_limit` | int | `3` | Identical fingerprints before `needs_human`. |
| `human_defer_seconds` | int | `120` | Skip a session a human touched this recently. |
| `extra_deny_patterns` | string | `""` | Comma-separated patterns that **extend** (never shrink) the hard-coded deny-list. |
| `enabled_repos` | repo-multiselect | `[]` | Limit governance to these repos (empty = all). |

## API + frontend

- **Global control** — free via registration: the generic automations API + `AutomationView`
  render autopilot's mode selector, config, `status()` text ("N advancing, M escalated"),
  and activity feed. No new global UI code.
- **Per-session opt-out** — new nullable `Session.autopilot_paused` column (+ `SessionRecord`
  + `autopilotPaused` in `_record_to_dict`); a small toggle on `SessionCard.tsx` reusing
  existing badge/control styling. Setting it goes through a new orchestrator method
  `set_autopilot_paused(session_id, paused)`.
  - **Daemon parity:** this is a new orchestrator method an API calls, so it needs a
    `RemoteOrchestrator` proxy + registered RPC (per the known "request-layer orchestrator
    methods need a daemon proxy" failure mode) or the breadth tests fail. Add both.

## Data flow

```
session enters "waiting_input" (tool permission)
  → orch._persist_status fires → autopilot nudge → AutopilotLoop.tick()
      → list_governed_sessions() includes it (not paused, actionable)
      → get_decision_context() : {kind: permission, tool: Bash, input: "pytest -q", goal ...}
      → policy.pre_decide() : not deny-listed, budget ok, no loop → unresolved
      → decider.decide() : {action: approve, reason: "safe test run, serves goal"}
      → policy.post_validate() : approve allowed → ok
      → (auto) ensure_driver → resolve_permission(allow) ; ledger++ ; emit "approved: pytest"
  → session resumes ; loop moves on
```

## Error handling

- **Any per-session step raises** → caught, logged, session skipped this tick (never crashes
  the loop). Mirrors `_sweep_one`.
- **Decider/model failure** → `escalate` (safe default). Never a mutation on uncertainty.
- **`ensure_driver` returns None** (unresumable) → `escalate` + `emit`; don't retry-storm.
- **Drain begins mid-tick** → the next tick's `_draining` check halts new actions; in-flight
  action for the current session completes (single, bounded).
- **Ledger persistence failure** → treat budget as exhausted for safety (skip), log.
- **Config invalid** → fall back to schema defaults (like `_backlog_read_config`).

## Testing (constitution V — TDD; mirrors backlog-runner tests)

All seams injected — **no network, no real model, no real sessions.**

1. **`autopilot_policy` (pure units):** each deny-list pattern denies; `--force-with-lease`
   is allowed; `allow_tool_approvals=false` blocks all approvals; human-deference skip;
   hourly/daily circuit-breakers; loop-detection → `needs_human`; recovery cap; post-validate
   downgrades an illegal `approve` and out-of-range `answer_index` to `escalate`.
2. **`autopilot_decider` (canned `_query_fn`):** valid JSON parses to `Decision`; malformed /
   empty / model-raises → `escalate`; guidance text is included in the system prompt.
3. **`AutopilotLoop.tick` (injected seams):** propose vs auto (propose emits, never mutates);
   `mode=off` and `_draining` no-op; governed-session filtering (paused/terminal excluded);
   nudge + interval never double-act (lock); ledger increments + fingerprint recorded.
4. **Effective goal:** `effective_goal` precedence (explicit `/goal` wins; else initial-prompt
   fallback); contract returns `(text, source)`.
5. **Daemon parity:** `set_autopilot_paused` present on `RemoteOrchestrator` + registered RPC
   (breadth test).
6. **Serialization / web:** `autopilotPaused` round-trips the nullable column; `SessionCard`
   opt-out toggle (vitest); `tsc --noEmit` + `eslint` green in `web/`.
7. **CI gates:** ruff + mypy + pytest locally before PR (mypy is an easily-missed gate).

## Build order

1. `autopilot_policy.py` (pure guardrails) + its unit tests — the safety core, TDD-first.
2. `autopilot_decider.py` + `autopilot_guidance.md` + decider tests (canned `_query_fn`).
3. `effective_goal()` helper (2-tier: explicit `/goal` → initial-prompt summary) + tests.
4. `Autopilot` metadata + `config_schema`; register in `registry.py` (gets UI + config free).
5. `AutopilotLoop` + injected orchestrator seams + `_autopilot_tick_loop` + `_persist_status`
   nudge; loop tests.
6. Per-session opt-out: `autopilot_paused` column + `set_autopilot_paused` + RemoteOrchestrator
   proxy/RPC + `SessionCard` toggle; tests + daemon-parity test.
7. Full suite (ruff + mypy + pytest + vitest + tsc + eslint) green; PR, ready + auto-merge.
