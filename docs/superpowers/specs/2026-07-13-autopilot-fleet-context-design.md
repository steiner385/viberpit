# Autopilot fleet context — cross-session awareness for the decider

**Date:** 2026-07-13 · **Status:** approved

## Problem

Autopilot judges each governed session in isolation: the `DecisionContext` carries only
that session's goal, stuck payload, transcript tail, PR facts, and reversal hint. The
decider cannot see that a sibling session is working the same repo/branch, that another
session already merged the fix it is about to nudge toward, or that a recently archived
session landed the goal's work. This produces duplicate work, collision-prone nudges,
and completion calls blind to fleet history.

## Decision

Give the decider a compact, pre-digested **fleet view** — advisory only (guardrails
unchanged), following the `reversal_hint` pattern of deterministic assembly feeding the
untrusted LLM.

### Shape (`DecisionContext.fleet_context: dict | None`)

- `same_repo_sessions` — active sessions in this session's repo, detailed:
  `{session, branch, status, goal, pr?}` (cap 8).
- `other_active_sessions` — one-line summaries of actives elsewhere (cap 10).
- `recently_archived` — sessions archived in the last 24h, newest first (cap 10):
  `{session, repo?, goal, outcome, hours_ago}` where `outcome` is verified PR
  ground-truth ("PR #591 merged") or `"archived"`.
- `None` when there is nothing to report.

### Components

- `server/automations/fleet_context.py` — pure shaping (`FleetSession`,
  `ArchivedFleetSession`, `build_fleet_view`, `pr_summary`). No I/O; every rule unit-tested.
- Orchestrator seams — `_autopilot_fleet_snapshot()` builds (active, archived) once per
  ~15s TTL (one archived-session DB query per 20s tick, not per session);
  `_autopilot_recent_archived()` queries `Session.archived_at >= now-24h` and reuses
  `_autopilot_pr_facts` on the DB row for outcomes (poller cache only, no new GitHub I/O);
  `_autopilot_fleet_context(sid)` is best-effort — any failure degrades to `None`.
- `autopilot_decider._build_prompt` — adds `"fleet"` to the JSON payload.
- `autopilot_guidance.md` — new `## fleet` section: avoid duplicate work, avoid branch
  collisions, reference landed sibling PRs in nudges; fleet is awareness only — never
  act on a sibling, never `complete` because a sibling met its goal.

## Alternatives rejected

- **Prose hint string** (a second `reversal_hint`): mushier signal for structured
  questions ("is PR #591 my branch?"), harder to test field-by-field.
- **Decider query tools**: breaks the proven one-shot/no-tools decider design; adds
  latency; untestable without a live model.
- **Deterministic branch-collision guardrail**: deferred until real collision patterns
  appear (risk of false escalations on reviewer/implementer pairs).

## Testing

- Pure: precedence of `pr_summary`, self-exclusion, same-repo vs other split, `None`
  repo never groups, caps, goal clipping, archived entry shape.
- Seams: `get_context` populates `fleet_context`; snapshot TTL caches the archived
  query; failure degrades to `None`; archived window/outcome/goal fallback.
- Decider: prompt includes the fleet block.
