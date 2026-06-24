# Session historic scrollback + transcript export — design

**Date:** 2026-06-23
**Status:** Approved (pre-implementation)
**Feature area:** 001-ccswitch-web — session transcript UI

## Problem

A session's transcript view only shows the **last 20 turns**. Replay is hardcoded to
`max_turns=20` in `server/sessions/replay.py:103`, so older history is unreachable — you
cannot scroll back to the start of a long session. There is also no way to export a
session's transcript for sharing, archiving, or re-ingestion.

We want:

1. **Full historic scrollback** — scroll up to the very first turn of a session, loaded
   lazily in chunks as the user scrolls (fast first paint, low memory on long sessions).
2. **Transcript export** — download a session as **Markdown** (human-readable) and as
   **raw JSONL** (lossless, truly unredacted copy of the SDK's on-disk transcript).

## Source of truth (unchanged)

The Claude SDK already writes the durable transcript to
`<config_dir>/projects/<project>/<claude_session_id>.jsonl`. It is located via
`orchestrator.transcript_path()` (robust UUID-glob with shared-base fallback). **No new
data is persisted** — both features are reads over this existing artifact. The 20-turn cap
is the only thing standing between the user and full history.

This keeps the change low-risk: it adds read paths over an existing, durable file and does
not touch session launch, PTY/driver behavior, or live streaming.

## Architecture context

- The transcript UI is **not** an xterm.js byte-stream terminal. It is a structured chat
  view: the browser rebuilds `Turn`/`Block` trees from typed wire events
  (`web/src/session/reducer.ts`), and the server reconstructs those events by parsing the
  on-disk JSONL (`server/sessions/replay.py`).
- On WS attach the server replays the last 20 turns as a single `batch` frame led by
  `replayReset` (idempotent on reconnect), then sends `replayComplete`
  (`server/bridge/ws.py:169-196`).
- A read-only `GET /api/sessions/{id}/transcript` already returns **redacted** wire events
  (`server/api/sessions.py:488-517`).
- Turn boundaries are explicit `TurnStart` events, so the JSONL can be sliced on whole-turn
  boundaries → history windows are **disjoint by turn ordinal**; no client-side overlap or
  dedup logic is required.

## Components

### 1. Backend — windowed replay (`server/sessions/replay.py`)

Refactor parsing so a caller can request a **turn-ordinal window** rather than only
"last N". Turns are indexed `0 .. totalTurns-1` from the start of the session.

- `_parse` already builds `starts` = indices of `TurnStart` in the flat event list;
  `totalTurns = len(starts)`.
- New: `replay_window(jsonl_path, *, before: int | None = None, limit: int = 20)` →
  `ReplayWindow(events, earliest_turn, total_turns)`:
  - `before is None` ⇒ keep the **last `limit` turns** (today's behavior; initial attach is
    unchanged and fast). `earliest_turn = max(0, total_turns - kept_count)`.
  - `before = K` ⇒ keep the `limit` turns whose ordinals are in `[max(0, K-limit), K)`.
    `earliest_turn` = the lowest ordinal in the returned window.
- The `SessionMeta` (model) event is hoisted to the front of **every** window (status strip
  needs the model after attach), exactly as today.
- `replay_events(path, *, max_turns=20)` is reframed as a thin wrapper over `replay_window`
  (or removed and call sites updated) — no parallel parser. The `_parse` core stays single.

Edge cases the window logic must handle: empty/zero-turn transcript (`total_turns == 0`,
empty events, `earliest_turn == 0`); `before` larger than `total_turns` (clamp); `before`
at or below the first kept ordinal (returns up to ordinal 0, `earliest_turn == 0`);
fewer than `limit` turns remaining before the cursor.

### 2. Backend — API surface (`server/api/sessions.py`)

**Paging** — extend the existing transcript endpoint with optional query params:

```
GET /api/sessions/{session_id}/transcript?before=<ordinal>&limit=<n>
→ { events: WireEvent[], earliestTurn: int, totalTurns: int }
```

- Events remain **redacted** (`redact_event(e.to_wire())`), same as today.
- **Backward compatible:** with no query params the response shape is unchanged for current
  callers (still `{ events }`); the new `earliestTurn`/`totalTurns` fields are additive.
  When `before`/`limit` are supplied, the windowed slice is returned.
- 404 if the session is unknown (active records or DB), preserving current behavior.

**Export** — two dedicated download endpoints:

```
GET /api/sessions/{session_id}/export.md     → text/markdown,        attachment
GET /api/sessions/{session_id}/export.jsonl  → application/x-ndjson, attachment
```

- `export.md`: built from the **redacted** full (untrimmed) wire-event list. A small
  server-side formatter renders, in timeline order: user turns, assistant text, thinking
  (as a collapsible/quoted block), tool calls (name + input), and tool results (ok/error +
  payload). Filename: `<session-name-or-id>-<claude_session_id>.md`.
- `export.jsonl`: streams the **raw, unredacted** on-disk `.jsonl` byte-for-byte as a
  download (per decision: truly raw — it is the user's own data, downloaded by an
  already-authenticated tailnet identity). Filename mirrors the `.md` with `.jsonl`.
- Both 404 when `transcript_path()` resolves nothing (no transcript on disk yet).
- Both require the same `current_identity` auth dependency as other session reads.

### 3. Backend — WS attach (`server/bridge/ws.py`)

Initial replay still sends the **last 20 turns**. `replayComplete` gains two fields so the
client immediately knows whether older history exists and from which ordinal to page:

```
{ "type": "replayComplete", "earliestTurn": <int>, "totalTurns": <int> }
```

These come straight from the `ReplayWindow` used for the attach replay. Live streaming and
the `replayReset`/`batch` flow are otherwise untouched. When there is no transcript,
`earliestTurn == 0` and `totalTurns == 0` (no "load older" affordance shown).

### 4. Frontend — reducer (`web/src/session/reducer.ts`)

Add to `TranscriptState`: `earliestTurn: number`, `totalTurns: number`,
`loadingOlder: boolean` (initial `0`, `0`, `false`).

- `replayReset`: also reset `earliestTurn`/`totalTurns` to `0` (history metadata is
  re-established by the following `replayComplete`).
- `replayComplete`: record `earliestTurn` and `totalTurns` from the frame.
- New synthetic action `prependHistory(events: WireEvent[], earliestTurn: number)`:
  folds the older wire-event window through the **existing reducer** into a throwaway
  `initialTranscript` state (`events.reduce(reducer, seeded)`), takes the resulting
  `.turns`, and returns `{ ...state, turns: [...olderTurns, ...state.turns],
  earliestTurn, loadingOlder: false }`. Reusing the reducer avoids a second turn-builder
  (no parallel implementation). A `loadingOlder` setter action guards re-entrancy.

The fold reuse may require exporting a small `buildTurnsFromEvents(events): Turn[]` helper
that runs the reducer over a seeded `initialTranscript` (with `replaying: true` so streaming
blocks settle correctly). This helper is the single turn-building path shared by live,
replay, and prepend.

### 5. Frontend — TranscriptView + wiring

`web/src/components/session/TranscriptView.tsx`:

- Add a **top sentinel** and extend the scroll handler: when the user scrolls near the top
  (e.g. `scrollTop <= threshold`) and `earliestTurn > 0` and not already `loadingOlder`,
  invoke `onLoadOlder()`.
- **Preserve scroll position across prepend:** before requesting, capture
  `el.scrollHeight`; in a layout effect after the prepend lands, set
  `el.scrollTop += (el.scrollHeight - prevScrollHeight)` so the viewport stays anchored on
  the same content (no jump-to-top).
- Render a small spinner at the top while `loadingOlder`, and a **"Beginning of session"**
  marker when `earliestTurn === 0`.
- The existing auto-scroll-to-bottom logic must NOT fire on a prepend (it keys off
  `turns`); guard it so only appends near the bottom auto-scroll. Track this with the
  near-bottom ref already present plus a "prepend in progress" flag.

API client (`web/src/api/…`) + types:

- `fetchOlderTurns(sessionId, before, limit)` → `{ events, earliestTurn, totalTurns }`.
- `exportTranscript(sessionId, format: 'md' | 'jsonl')` → triggers a browser download
  (anchor with `download` attr / blob), hitting the export endpoints.
- An **Export** control in the session header (menu with two items: Markdown, Raw JSONL).

### 6. Interactions / known limitations

- **Rotation markers** are live-only state preserved across `replayReset` and anchored by
  turn id. Paged-in older turns re-anchor automatically in `TranscriptView`
  (`groupRotationsByAnchor`) — no extra handling.
- **Sub-agent threads** (keyed by Task toolId) come from sidechain entries that `_parse`
  skips. Older Task tool calls render, but their sub-threads stay collapsed/empty — same as
  replayed history today. Not solved here.
- **Re-parse cost:** each page re-parses the full JSONL (O(file)). Acceptable for typical
  transcripts; a parsed-window cache is a noted future optimization if very large sessions
  appear. If added, surface any truncation rather than silently capping.

## Testing (TDD)

Server (`pytest server/tests/`):
- `replay_window`: correct ordinals & `totalTurns`; disjoint consecutive windows reconstruct
  the full transcript; edge cases (empty transcript, `before` past end, fewer-than-limit
  before cursor, `before` reaching ordinal 0); `SessionMeta` present in every window.
- Transcript endpoint paging: `?before/&limit` returns the right window + metadata;
  no-params response unchanged; 404 unknown session.
- Export: `export.md` renders user/assistant/thinking/tool-call/tool-result sections in
  order; `export.jsonl` returns raw bytes with `Content-Disposition: attachment` and the
  right media type; both 404 when no transcript file; auth (401 without allowlisted
  identity).

Web (`vitest run`, `tsc --noEmit`, `eslint` in `web/`):
- Reducer: `prependHistory` prepends older turns in correct order, lowers `earliestTurn`,
  clears `loadingOlder`; `replayComplete` records metadata; `replayReset` clears it.
- `buildTurnsFromEvents` produces the same turns as a live fold for a sample event sequence.
- API client `fetchOlderTurns` / `exportTranscript` request shape.
- Scroll handler: triggers `onLoadOlder` only when near top, `earliestTurn > 0`, not already
  loading; auto-scroll-to-bottom does not fire on prepend.

## Out of scope

- xterm.js-style raw PTY byte scrollback (the UI is event-based; not applicable).
- Persisting our own transcript copy or ring buffer (SDK JSONL is the source of truth).
- Normalized-JSON export format (only Markdown + raw JSONL were requested).
- Redacting the raw JSONL export (explicitly chosen to be truly raw).
- Server-side parsed-window caching (noted as future optimization only).
