# `/goal` Conformance Audit + Goal Indicators — Design

**Date:** 2026-06-22
**Feature area:** 001-ccswitch-web (web session manager)
**Status:** Approved design, pending implementation plan
**Builds on:** `2026-06-22-native-slash-commands-design.md` (catalog + dispatcher are the
authority for how a command executes; this spec extends the dispatcher for `/goal`).

## Problem

`/goal` is a **real, built-in Claude Code feature** (verified against the binary at
`versions/2.1.186`, not assumed): `/goal <condition>` sets a session objective, the CLI
tracks internal `active_goal` state, evaluates whether the condition is met, and
`/goal clear` stops it. It only works in a **trusted workspace** and is disabled when
hooks are restricted.

Today the ccswitch web app cannot tell the user **which sessions have an active goal or
what it is**:

- `/goal` is classified `passthrough` in `server/sessions/commands/catalog.py` — when
  the UI sends it, the dispatcher forwards `"/goal <args>"` to the SDK as a normal turn
  and tracks nothing (`server/sessions/commands/dispatcher.py`).
- There is **no goal field** anywhere: not on the `Session` DB model
  (`server/db/models.py`), `SessionRecord` (`server/sessions/types.py`), the API
  serialization (`server/api/sessions.py` `_record_to_dict`), or the `Session`
  TypeScript type (`web/src/api/types.ts`).
- So the frontend has nothing to render.

### Architectural constraint that shapes everything

`/goal` state is **not observable through any structured SDK channel.** Verified by code
inspection of the installed `claude-agent-sdk==0.2.105`: no `SystemMessage` subtype, hook
event, or message class carries goal/objective/`active_goal` data. The feature lives
entirely inside the CLI binary and surfaces only as text. Therefore an indicator is only
as trustworthy as our ability to *capture* the goal ourselves — capture is the core of
this work, not display.

## Goals

- A reliable server-side record of each session's **active goal text**.
- A minimal frontend indicator: a **"Goal" badge + tooltip** on each `SessionCard`.
- End-to-end conformance: confirm `/goal` actually engages via our SDK path, is
  classified correctly, and is not blocked by workspace-trust gating.

## Non-goals (v1, YAGNI)

- Setting/clearing goals from a dedicated UI control (still typed as `/goal …`).
- Goal **history** (set/met/cleared timeline) or an expandable panel.
- **Auto-met / auto-cleared** detection (requires text parsing — see Channel B below).
- Capturing goals typed **directly in a raw terminal** outside our dispatcher.
- A new live WebSocket message for goals — the existing REST poll is sufficient for v1.

## Observation channels (verified) and the chosen one

Ranked from research against the real SDK and codebase:

- **Channel A — native SDK event.** *Not available.* No goal-bearing message type in
  `claude-agent-sdk==0.2.105`.
- **Channel B — text-parse the assistant/CLI output** for "Goal set/met/cleared". Would
  catch auto-met and raw-terminal goals, but is fragile, and whether `/goal` confirmation
  even surfaces as captured transcript text is **unverified**. **Deferred (out of scope).**
- **Channel C — intercept at our command dispatcher (CHOSEN).** When the web UI sends
  `/goal <args>`, the dispatcher sees the text before forwarding. 100% reliable for
  UI-set goals; the natural single authority (consistent with the native-slash-commands
  design, where the dispatcher owns *how* a command executes).

Tradeoff accepted: Channel C does not see goals typed in a raw terminal or goals
auto-met by the CLI. That is the v1 boundary; Channel B can extend it later.

## Phase 0 — Conformance audit (gates everything; do FIRST)

This is a **verification spike** in the spirit of constitution III and the
native-slash-commands spec's spike. Before building UI:

1. **Does `/goal` actually engage through the SDK input path?** Confirm against the real
   binary whether sending `"/goal <condition>"` via the dispatcher's existing passthrough
   (`driver`'s `send_prompt`/`query`) genuinely activates the built-in goal feature, or
   merely sends literal text to the model. **If it does not engage, the indicator would
   be cosmetic** — surface this immediately and reassess before continuing.
2. **Workspace-trust gating.** Sessions are pre-trusted via `configdir.trust_project()`
   (orchestrator), so `/goal` should be available — verify it is not blocked.
3. **Catalog classification.** Confirm `/goal`'s `kind` in `catalog.py` matches the
   engagement model found in (1); fix if mismatched.

The spike's outcome decides whether Phase 1/2 ship as-is or need adjustment.

## Phase 1 — Backend goal state

- Add `active_goal: str | None = None` to `SessionRecord` (`server/sessions/types.py`).
- Add a **nullable** `active_goal` column to the `Session` DB model
  (`server/db/models.py`). Nullable ⇒ `init_db` backfills it, no migration needed in dev
  (per CLAUDE.md dev-SQLite note).
- Include `activeGoal` in the wire dict in `server/api/sessions.py` `_record_to_dict`.
- In `server/sessions/commands/dispatcher.py`, detect `cmd.name == "goal"`:
  - `/goal clear` (or empty) → set `active_goal = None`.
  - `/goal <condition>` → set `active_goal = "<condition>"`.
  - Persist through the **same path that already persists `status`/`model`** changes
    (no parallel write path — constitution VI), **then** forward the command as before so
    the CLI still receives it.

## Phase 2 — Frontend indicator (minimal: badge + tooltip)

- Add `activeGoal?: string | null` to `Session` in `web/src/api/types.ts`.
- In `web/src/components/SessionCard.tsx`, render a small **"Goal" pill** in the existing
  badge row (alongside status/usage/account) **only when `activeGoal` is set**; a tooltip
  reveals the full goal text. Reuse the existing badge / `StatusPill` styling and design
  tokens — no new visual primitives (constitution VI; web "find existing source of
  truth" norm).
- The badge updates via the existing `useSessions()` 5s REST poll — no new hook, no new
  WS message.

## Data flow

```
UI sends "/goal ship the PR"
  → WS command frame (existing native-slash-commands path)
  → dispatcher detects name=="goal" → active_goal="ship the PR"
        → persist to SessionRecord + DB (existing status/model persistence path)
        → forward "/goal ship the PR" to CLI as before
  → GET /api/sessions (5s poll) → _record_to_dict includes activeGoal
  → useSessions() → SessionCard renders "Goal" pill + tooltip
```

## Error handling

- **Empty/whitespace goal** → treated as no goal (`active_goal = None`); pill hidden.
- **Persistence failure** → log; command still forwards to the CLI (capture is
  best-effort and must never block the actual `/goal` execution).
- **`activeGoal` absent in the wire payload** (older record) → frontend treats as null;
  no pill. Forward/backward compatible.

## Testing (constitution V — TDD; III — verify against real SDK)

Run against the main checkout's prebuilt env per CLAUDE.md; use the
`CCSWITCH_DEV=1` / allowlist harness for API tests.

1. **Phase 0 spike** — real-SDK probe that `/goal` engages and is trust-permitted.
   Documented finding gates the rest.
2. **pytest (dispatcher):** `/goal foo` → record `active_goal == "foo"`; `/goal clear`
   → `None`; empty args → `None`; command is still forwarded.
3. **pytest (serialization):** `_record_to_dict` emits `activeGoal`; round-trips through
   the DB nullable column.
4. **vitest (`SessionCard`):** pill + tooltip shown when `activeGoal` set; hidden when
   null/empty. Mind the dashboard partial-mock pitfall noted in project memory if a new
   hook were added (none is, by design).
5. `tsc --noEmit` + `eslint` green in `web/`.

## Build order

1. Phase 0 verification spike (gates everything).
2. Backend: `SessionRecord` + DB column + `_record_to_dict` field.
3. Backend: dispatcher `/goal` capture + persistence; tests.
4. Frontend: `Session` type + `SessionCard` pill/tooltip; tests.
5. Full suite (pytest + vitest + tsc + eslint) green; PR.
