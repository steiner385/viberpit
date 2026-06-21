# Session status strip — design

**Date:** 2026-06-21
**Feature branch:** `feature/session-status-strip`

## Goal

The SessionView should show, at a glance: the **current account**, **current model**,
**usage stats**, **remaining context**, and the **list of sub-agents**.

## Form factor (decided)

Expand the SessionView top bar into **two tiers**:

- **Tier 1 (unchanged):** `← Back · title · StatusBar`
- **Tier 2 (new) — `SessionMeta` strip:** a compact, always-visible row:

  `account ⟳ · model · ctx 78% · sess 42% / wk 18% · 🟢 2 agents`

  On narrow screens it degrades to `account · model · ctx%`. The agents segment is a
  button that toggles an inline `SubAgentList` popover.

## Data sources

| Item | Source | Status |
|---|---|---|
| Current account | `useSessions()` record `accountName`, overridden by the latest `rotated` WS event | exists |
| Current model | SDK `SystemMessage(init).data["model"]` → new `sessionMeta` event; replay re-emits from transcript `message.model` | NEW |
| Session usage (tokens/$) | `ResultMessage.usage` → `turnEnd.usage`, accumulated in reducer | partial (event flows; reducer ignores it) |
| Remaining context | `modelContextWindow(model) − (input + cache_read tokens of last turn)`, as a % | NEW (derived) |
| Account quota | `useAccounts()` record `usage.session.pct` / `usage.weekly.pct` | exists |
| Sub-agents | `Task` tool calls scanned from `state.turns`, paired with their tool results for status | NEW (derived) |

## Wire change

New session event (must be added to `SESSION_EVENT_TYPES` so the WS fan-out routes it):

```
sessionMeta { type: 'sessionMeta'; model: string; sessionId: string }
```

- Emitted live by `translator.py` on `SystemMessage(subtype="init")` when `data` carries a model.
- Re-emitted on re-attach by `replay.py` from the first assistant message's `model` field.
- `turnEnd.usage` is also populated by replay (read `message.usage` instead of `None`).

## Components & modules (each independently testable)

**Backend**
- `events.py` — `SessionMeta` dataclass + `to_wire()`.
- `translator.py` — emit `SessionMeta` on init (skip when no model).
- `replay.py` — emit `SessionMeta` once; carry `usage` into `TurnEnd`.
- `tests/fakes/fake_sdk.py` — init `data` includes `model` so tests exercise the path.

**Frontend**
- `session/events.ts` — `EvSessionMeta`, union member, `SESSION_EVENT_TYPES` entry.
- `session/reducer.ts` — state gains `model: string | null` and
  `usage: { input; output; cacheRead; costUsd } | null`; handle `sessionMeta` and accumulate `turnEnd.usage`.
- `lib/contextWindow.ts` — pure `modelContextWindow(model)` (200k default, 1M for `[1m]` ids)
  and `remainingContextPct(usage, model)`.
- `session/selectors.ts` — `selectSubAgents(turns)` → `{ toolId, type, description, status }[]`
  where status ∈ `running | done | failed` (from presence/ok of the paired tool result).
- `components/session/SessionMeta.tsx` — presentational strip (all values pre-derived as props).
- `components/session/SubAgentList.tsx` — expandable popover listing sub-agents.
- `components/session/SessionView.tsx` — wire it: read `useSessions`/`useAccounts`, latest rotation,
  reducer-derived model/usage/sub-agents.

## Edge handling

- Unknown model → context shows `—`; strip still renders.
- No `turnEnd` yet → usage/context `—`.
- Account quota `null` (non-Max account) → omit the quota segment.
- No sub-agents → omit the agents segment entirely (no "0 agents").

## Testing (TDD, per constitution Principle V)

- Reducer: `sessionMeta` sets model; `turnEnd` accumulates usage across turns.
- `contextWindow`: window lookup + remaining-% math + clamping + null usage.
- `selectSubAgents`: running vs done vs failed; non-Task tools ignored.
- `translator`: init with/without model.
- `replay`: emits `sessionMeta` + usage.
- `SessionMeta` render test: shows account/model/context, omits null quota.

## Out of scope (YAGNI)

- Live drill-in of a sub-agent's internal steps (SDK only surfaces spawn + final result).
- Historical usage charts / cost over time.
