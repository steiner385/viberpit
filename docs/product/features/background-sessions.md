---
slug: background-sessions
title: Background (maintenance) sessions
state: live
links:
  code:
    - server/automations/base.py
    - server/automations/registry.py
    - server/api/sessions.py
    - server/api/automations.py
    - web/src/session/backgroundSessions.ts
    - web/src/features/sessions/useSessionListState.ts
    - web/src/features/sessions/SessionListColumn.tsx
    - web/src/features/sessions/ScopeSegment.tsx
    - web/src/session/sessionNavState.ts
  prs: []
---

# Background (maintenance) sessions

**Problem** — ViberPit spawns routine maintenance sessions of its own: `pm map`,
`pm classify`, `pr-doctor fix`, control-band diagnoses, scheduled runs. They ran in the
Active list beside the work the operator was steering, inflating its count and competing
for attention while doing nothing that needed a human.

**Target users** — The operator scanning the Sessions list for what needs them next.

## The split

The sessions list has three scopes: **Active | Background | Archived**. Background holds
*quiet maintenance sessions*; everything else live is Active.

Two facts decide which side a live row is on:

1. **Work class.** Every automation class declares `work_class = "development" |
   "maintenance"` (`server/automations/base.py`, read only through `work_class_of`). The
   sessions API publishes it per session as `workClass`, derived from `Session.origin`
   through the registry, so the browser keeps no list of which origins are housekeeping.
   A human session, an unregistered recurring id, and an automation that forgot to declare
   all resolve to `development` — the safe mistake is a maintenance session that stays
   visible, never new work that is hidden.

   | class | automations |
   |---|---|
   | development | backlog-runner, sdlc-intake, sdlc-synthesizer |
   | maintenance | pr-doctor, pm-onboard, pm-classify, control-band-monitor, and every non-spawning automation |

2. **Triage tier.** A maintenance session is background only while its tier is `busy`,
   `waiting` or `complete` (`QUIET_TIERS` in `web/src/session/backgroundSessions.ts`). Any
   other tier — a permission card, a question, a plan approval, a parked model upgrade, a
   failure, a red PR, an unreachable host, or a turn that ended without completing — **lifts**
   it into Active until resolved, then it drops back. The read is the same `triageTier` the
   card pill and group headers use, so a row can never be quiet here while its card pulses.

Because lifting is the only attention path, the Background scope carries no badge of its
own, the tab-title "(N) needs you" count is unchanged, and the Sessions nav badge counts the
Active scope only.

## UI

- `ScopeSegment` gains the Background link with its count. Scope is route-derived, as for
  Archived: `/sessions/background`, `/sessions/background/:id`,
  `/sessions/background/:id/agents/:agentId`. The detail routes keep the live param name
  `id` and mount the normal session runtime — a background session is a live session the
  list merely keeps out of the way. The prefix exists so the column stays in Background,
  row highlighted, while one is open.
- `BackgroundScopeBody` is the Active body over the held-out rows through its own
  pipeline (`useBackgroundListState`): same cards, sections, filters, search, group and
  sort, persisted under the `background` namespace so its collapse state is separate.
- A lifted card shows the automation's name on line 1 (`card-maintenance-chip`) so it is
  obvious that "PR Doctor" is asking, not the operator's own work.
- Provenance labels (`originLabel`, `assistedByLabel`) are the registry's display names,
  published on the session; the card's hand-copied id→name map is gone.

## Not changed

Push notifications, the assistant's prose definition of "needs attention", and the server's
list endpoint (no origin filter; the split is client-side over the same poll).
