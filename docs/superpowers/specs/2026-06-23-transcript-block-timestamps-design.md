# Per-block timestamps in the session transcript

**Date:** 2026-06-23
**Feature:** 001-ccswitch-web
**Status:** Approved design

## Problem

The session transcript shows *what* happened but not *when*. Users want a small,
unobtrusive timestamp on each entry so they can tell how long a tool ran, when a
turn arrived, or how stale a reloaded session is.

## Scope

Add an authoritative, server-stamped timestamp to each transcript **block** and
render it as a small, always-visible, muted time in a fixed-width left gutter.

- Granularity: **per block** (each assistant text paragraph, thinking block, tool
  call, tool result, permission, question, inline error, user message).
- Source of truth: **server-side**. Live events self-stamp at construction; replayed
  events carry the real timestamp read from the Claude SDK `.jsonl` transcript.
- Semantics: the timestamp marks **when the block first appeared** — streaming text
  deltas and a later `toolResult` never overwrite the original stamp.
- Display: absolute, browser-local `HH:MM`, always visible, muted/tiny, with full
  `HH:MM:SS` + date on hover/tap via a `title` tooltip.

### Out of scope (YAGNI)

Per-turn headers, relative ("2m ago") times, per-wrapped-line stamps, a timezone
preference, any settings toggle, gutters inside nested sub-agent threads, and a DB
migration (transcripts are file-based and replayed from the SDK `.jsonl`).

## Key facts established during design

- The transcript is **not** xterm.js. Backend Claude-SDK events stream over WebSocket
  and `web/src/session/reducer.ts` folds them into Turns → Blocks rendered as React
  components (`web/src/components/session/Turn.tsx`).
- ccswitch does **not** persist transcript events to its own DB. On reload the
  transcript is rebuilt from the SDK `.jsonl` by `server/sessions/replay.py`. **No DB
  migration is required.**
- Each `.jsonl` transcript line already carries a top-level ISO-8601 `"timestamp"`
  (verified: e.g. `2026-06-23T12:12:52.806Z`). `replay.py` currently ignores it.
- Backend events are flat dataclasses with trailing optional fields and per-class
  `to_wire()`. There is no base class.

## Design

### 1. Backend — `server/sessions/events.py`

Add a shared epoch-milliseconds helper and a `ts` field to the **block-bearing**
events, included in each `to_wire()`:

- Helper: `def _now_ms() -> float: return time.time() * 1000`.
- Field on each block-bearing event: `ts: float = field(default_factory=_now_ms)`.
  Because it is a `default_factory`, **live** events stamp themselves at construction
  with no change to `driver.py` / `translator.py`; replay can still override via
  `ts=...` keyword.
- Events that gain `ts` (the ones that create a block): `UserMessage`, `TextDelta`,
  `ThinkingDelta`, `ToolCall`, `ToolResult`, `PermissionRequest`, `Question`,
  `ErrorEvent`. Each `to_wire()` adds `"ts": self.ts`.
- Events that do not create a block (`TurnStart`, `TurnEnd`, `SessionMeta`, `Title`,
  `Status`, `Rotated`, `QueueUpdate`, `Suggestions`, `ReplayComplete`,
  `TurnFailure`, `RateLimit`) are left unchanged.

`field` requires `from dataclasses import field` and `import time`.

### 2. Backend — `server/sessions/replay.py`

Read the transcript line's own time so reloaded blocks show the real original time:

- Add `def _ts_ms(o: dict) -> float | None:` that parses the line's ISO-8601
  `o.get("timestamp")` (`...Z` → `+00:00`) into epoch ms, returning `None` on
  absence / parse failure.
- Compute `ts = _ts_ms(o)` once per line and pass `ts=ts` to every block-bearing
  event constructed for that line (`UserMessage`, `ToolResult`, `TextDelta`,
  `ThinkingDelta`, `ToolCall`, `Question`). When `ts is None` the event's
  `default_factory` would fill "now"; to keep replayed history honest, pass the
  parsed value explicitly and let it be `None`-safe at the wire/UI layer (a missing
  `ts` renders an empty gutter rather than a fake "now"). Implementation note: pass
  `ts` through even when `None`; `to_wire()` emits whatever it holds.

### 3. Frontend types — `web/src/session/events.ts`

Add `ts?: number` (optional → legacy wire events and existing tests stay valid) to:
`EvUser`, `EvTextDelta`, `EvThinkingDelta`, `EvToolCall`, `EvToolResult`,
`EvPermissionRequest`, `EvQuestion`, `EvError`. No change to `SESSION_EVENT_TYPES`
or `web/src/ws/client.ts` (the field passes through transparently).

### 4. Frontend reducer — `web/src/session/reducer.ts`

- Add `ts?: number` to every variant of the `Block` union.
- Stamp `ts` from the event **only when a block is first created**, in BOTH the
  sub-agent path (`applyToThread`) and the main `transcriptReducer` switch:
  - `user` → `userTurnBlocks` takes and applies `ev.ts` to its text/image blocks.
  - `textDelta` / `thinkingDelta` → set `ts: ev.ts` in the *"open a new block"*
    branch only; the append branch leaves `ts` untouched.
  - `toolCall` → `ts: ev.ts` on the new tool block. `toolResult` does **not** touch
    `ts` (preserves first-appeared).
  - `permission`, `question`, inline `error` → `ts: ev.ts` on creation.
- `userTurnBlocks` gains a `ts?: number` parameter so every user-turn path stamps
  identically (single source).

### 5. Frontend rendering — `web/src/components/session/`

Introduce one shared gutter wrapper rather than editing every block component
(avoids N parallel implementations):

- New `BlockRow.tsx`: `flex` row with a fixed-width left `<time>` gutter
  (`text-xs text-muted-foreground tabular-nums`, `HH:MM`, `title` = full
  `HH:MM:SS` + date) and a `flex-1` content column for `children`. A `null`/absent
  `ts` renders an empty, same-width gutter so all rows stay vertically aligned.
- New `formatBlockTime.ts` util (after grepping `web/src/` for an existing time
  helper / design token and reusing it if found): `(ts?: number) => { short, full }`
  in browser-local time; returns empty strings when `ts` is absent.
- `Turn.tsx` wraps each rendered `item` in `<BlockRow ts={...}>`. The representative
  `ts` for a `toolGroup` item is its first tool's `ts`; for a single-block item it is
  `block.ts`. Sub-agent (nested) threads are unchanged in v1.

### 6. Tests (TDD — write first)

- **Backend** `server/tests/test_events.py`: `to_wire()` of each block-bearing event
  includes `ts`; a live event's `ts` is a positive number.
- **Backend** `server/tests/` (replay): `replay.py` parses the ISO timestamp into ms
  and attaches it to reconstructed events; a line missing `timestamp` yields
  `ts is None` and does not crash.
- **Frontend** `web/src/session/reducer.test.ts`: first event stamps the block's
  `ts`; subsequent `textDelta` appends and a later `toolResult` do **not** overwrite
  it; sub-agent path stamps too.
- **Frontend** render test: `BlockRow` shows the formatted local `HH:MM`, exposes the
  full time in `title`, and renders an aligned empty gutter when `ts` is absent.

## Data flow (after change)

```
Live:    SDK msg → translator/driver builds Ev*(ts=now_ms) → to_wire {ts} → WS
                 → reducer stamps Block.ts on creation → BlockRow renders HH:MM
Reload:  .jsonl line {timestamp} → replay._ts_ms → Ev*(ts=real) → to_wire {ts}
                 → reducer → BlockRow renders the original HH:MM
```

## Risks / mitigations

- **Same-minute blocks look identical** → full `HH:MM:SS`+date in the `title`
  tooltip disambiguates without widening the gutter.
- **Missing `ts`** (legacy/edge) → empty aligned gutter, never "Invalid Date".
- **Mobile discoverability of hover tooltip** → the always-visible `HH:MM` is the
  primary affordance; the tooltip is a precision bonus, not the only surface.
