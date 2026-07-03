# Inferred Session Goals — LLM-Derived Goal Intelligence — Design

**Date:** 2026-07-02
**Feature area:** 001-ccswitch-web (web session manager)
**Status:** Approved design, pending implementation plan
**Builds on:**
- `2026-06-22-goal-conformance-and-indicators-design.md` — the existing explicit `active_goal`
  (`/goal`) capture path and the `SessionCard` goal pill this feature complements.
- `2026-06-21-ai-session-titles-design.md` / the `_titling_monitor` loop — the established
  "LLM-derived session intelligence" pattern (`orchestrator.py:1740`) this sweep mirrors.
**Consumed by (optional):**
- `2026-07-02-autopilot-design.md` — autopilot judges completion via `effective_goal()`; this
  feature inserts the `"inferred"` tier into that helper. Neither feature blocks the other.

## Problem

ccswitch derives session intelligence with cheap LLM passes today: **titles** (auto-naming)
and **summaries**. It does **not** derive a session's *goal*. The only goal signal is
`active_goal` — the built-in `/goal` condition — which is **explicit and user-set**, so it is
`None` for the large majority of sessions that were simply started with a prompt.

That leaves a gap: nothing states, in one sentence, *what a session is trying to accomplish*
when the user never typed `/goal`. This matters for (a) at-a-glance dashboard comprehension and
(b) any consumer that must judge "is this session done?" against intent rather than a workflow
milestone — most immediately, the autopilot feature. The user asked for an "updated session
goal statement as part of our regular LLM-driven session intelligence."

## Goals

- A server-side **`inferred_goal`**: a concise (one-sentence) LLM-derived statement of what a
  session is trying to accomplish, maintained continuously as the session evolves.
- A single **`effective_goal()`** resolver that unifies the goal sources with clear precedence,
  so every consumer reads goal through one path (no parallel goal lookups).
- A minimal, non-intrusive **frontend indicator** distinguishing an *inferred* goal from an
  *explicit* `/goal`.
- Reuse the existing intelligence-loop, persistence, and serialization seams — no new
  orchestration primitives, no new live WS message.

## Non-goals (v1, YAGNI)

- **Not** auto-setting the built-in `/goal` — `inferred_goal` is a *separate, advisory* field.
  It never writes `active_goal` and never issues a `/goal` command to the CLI.
- No goal **history** / timeline; `inferred_goal` is a single current-value column.
- No text-parsing of CLI "goal met" output (the goal-conformance spec's deferred Channel B).
- No user editing of the inferred goal (it is derived; typing `/goal` sets the explicit one,
  which then wins).
- No new WebSocket frame — the existing 5s REST session poll surfaces it.

## Design

### Data model

- New **nullable** column `inferred_goal: str | None` on the `Session` DB model
  (`server/db/models.py`). Nullable ⇒ `init_db` backfills on dev SQLite with no migration
  (per CLAUDE.md dev-SQLite note); a real migration accompanies it for prod/Postgres.
- Mirror on `SessionRecord` (`server/sessions/types.py`): `inferred_goal: str | None = None`,
  including it in `to_dict`/`from_dict` as `inferredGoal` (forward/backward compatible — an
  absent key reads as `None`).
- Serialize `inferredGoal` in `server/api/sessions.py` `_record_to_dict`.

These sit **beside** `active_goal`, never replacing it.

### The `effective_goal()` resolver (single source of truth)

One helper answers "what is this session's goal," with precedence highest-first:

```
def effective_goal(record) -> tuple[str, str]:      # (goal_text, source)
    if record.active_goal:    return record.active_goal, "explicit"        # user /goal — wins
    if record.inferred_goal:  return record.inferred_goal, "inferred"      # LLM-derived
    return initial_prompt_summary(record), "initial_prompt"                 # fallback
```

If the autopilot feature already shipped its 2-tier version of this helper, **this feature
edits that helper in place** to insert the middle tier — it does not add a second resolver
(constitution VI / "eliminate parallel implementations"). If this feature ships first, it
introduces the 3-tier helper and autopilot consumes it as-is.

### Goal-intelligence sweep (`_goal_monitor`)

A background loop peer of `_titling_monitor`, registered through the same `_spawn` task
registry so `stop()` cancels it (issue #405 leak discipline):

- **Cadence:** a periodic sweep (`_goal_interval`, default ~120s) over active, non-terminal
  sessions, **plus** a turn-boundary refresh (piggyback the existing `done` transition that
  already drives titling) so a session's goal sharpens as real work lands.
- **Per session:** a cheap one-shot LLM call (mirroring `rank_issues`: `model="haiku"`,
  `allowed_tools=[]`, `setting_sources=[]`, injected `_query_fn` + `CLAUDE_CONFIG_DIR`/`cwd`,
  so the real model is **never** called in tests). Input = the initial prompt + a transcript
  tail; output = one sentence. Persist via the same path that writes `status`/`model`/
  `active_goal` (no parallel write path — reuse the `_persist_goal`-style updater, generalized
  to the new column).
- **Gating (cost/quality):**
  - Skip sessions with an explicit `active_goal` (explicit already wins — inferring is wasted).
  - Skip if unchanged: only recompute when the transcript has advanced since the last inference
    (track a cheap marker — e.g. last-seen turn count — to avoid re-billing an idle session).
  - Best-effort: any failure logs and **leaves the prior `inferred_goal` intact** (never
    clobbers a good value with a failed call).

### Frontend indicator

- Add `inferredGoal?: string | null` to `Session` in `web/src/api/types.ts`.
- In `SessionCard.tsx`, when there is **no** explicit goal but `inferredGoal` is set, render a
  lighter-weight "Goal" pill variant (e.g. a subdued/outline style + an "inferred" affordance
  in the tooltip) distinct from the solid explicit-`/goal` pill. Reuse the existing pill /
  `StatusPill` styling and design tokens — no new visual primitive (web "find the existing
  source of truth" norm). Explicit `/goal` continues to render exactly as today and takes
  visual precedence.
- Updates via the existing `useSessions()` 5s poll — no new hook, no new WS message.

## Data flow

```
session runs turns
  → turn-boundary "done" (existing titling trigger) OR periodic _goal_monitor sweep
      → skip if active_goal set, or transcript unchanged since last inference
      → one-shot Haiku over initial prompt + transcript tail → "Fix the flaky batch-unarchive test"
      → persist inferred_goal (existing status/model persistence path) ; update marker
  → GET /api/sessions (5s poll) → _record_to_dict includes inferredGoal
  → useSessions() → SessionCard renders subdued "Goal (inferred)" pill (only when no explicit /goal)

any consumer (e.g. autopilot):
  effective_goal(record) → explicit active_goal, else inferred_goal, else initial-prompt summary
```

## Error handling

- **Model/parse failure** → log; keep the prior `inferred_goal` (best-effort, never clobber).
- **Persistence failure** → log; the sweep retries next cadence (idempotent write).
- **`inferredGoal` absent in wire payload** (older record) → frontend treats as `null`; no
  pill. Forward/backward compatible.
- **Explicit goal set mid-session** → next resolve returns the explicit goal; the inferred pill
  yields to the explicit one automatically (no explicit clear of `inferred_goal` needed).

## Testing (constitution V — TDD; III — verify against real SDK)

Run against the main checkout's prebuilt env per CLAUDE.md; API tests use the
`CCSWITCH_DEV=1` / allowlist harness. All LLM calls injected — **no network, no real model.**

1. **pytest (resolver):** `effective_goal` precedence — explicit > inferred > initial-prompt;
   returns `(text, source)`; empty/whitespace fields fall through.
2. **pytest (`_goal_monitor`, canned `_query_fn`):** writes `inferred_goal` from initial prompt
   + transcript; **skips** when `active_goal` set; **skips** when transcript unchanged;
   model failure leaves the prior value intact; loop registered in the `_spawn` task set.
3. **pytest (serialization):** `_record_to_dict` emits `inferredGoal`; round-trips the nullable
   column; absent key reads as `None`.
4. **vitest (`SessionCard`):** inferred pill shown only when `inferredGoal` set **and** no
   explicit goal; explicit `/goal` still takes precedence and its styling is unchanged; hidden
   when both null. Mind the dashboard partial-mock pitfall in project memory (no new hook).
5. `tsc --noEmit` + `eslint` green in `web/`; ruff + mypy + pytest locally before PR.

## Build order

1. `effective_goal()` resolver (3-tier) + resolver tests. (If the autopilot 2-tier helper
   already exists, edit it in place to add the `inferred` tier.)
2. Backend data: `inferred_goal` column (+ prod migration) + `SessionRecord` +
   `_record_to_dict` `inferredGoal`; serialization tests.
3. `_goal_monitor` sweep + turn-boundary refresh + gating + persistence; loop tests.
4. Frontend: `Session` type + `SessionCard` inferred-pill variant; vitest.
5. Full suite (ruff + mypy + pytest + vitest + tsc + eslint) green; PR, ready + auto-merge.
