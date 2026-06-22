# Eager Session Creation — Design

**Date:** 2026-06-21
**Status:** Approved (design), pending implementation plan
**Branch:** `002-eager-session-create`

## Problem

When a user creates a new session via the UI, the experience lags behind the
user's intent:

- The **New Session dialog** closes and leaves the user on the dashboard. They
  must find and click the new card to enter the conversation.
- The new session **card** does not appear until the next 5s `useSessions`
  poll (up to a 5-second delay).
- The **conversation view** is blank until the agent's first response streams
  back, even though the user already typed their initial prompt.

The user should see their session slot and conversation thread immediately on
creation, not after the first agent response.

## Key finding: the slot already exists; the gap is UX + visibility

Investigation showed the backend **already** creates the session slot eagerly:
`orchestrator.create_session()` inserts the DB row (`status="idle"`,
`claude_session_id=NULL`) and returns `201` *before* the agent/SDK turn runs.

The initial prompt is **intentionally deferred**: it is stashed in
`_pending_initial_prompt[session_id]` and only sent when the first WebSocket
client attaches (`server/bridge/ws.py`). The orchestrator comment explains why —
sending the prompt with no consumer watching causes the turn's events to
accumulate in `driver._out` and race the transcript replay when a client later
attaches. Deferring until attach guarantees the client is already watching, so
events stream live and nothing is lost.

**This deferral is a feature, not a bug — and we keep it.** Auto-opening the
session on creation triggers a WebSocket attach within milliseconds, which fires
the pending prompt with the client already watching. The thread is therefore
"underway at creation time" *without* removing the live-stream safety mechanism.

There is also an existing inconsistency: the `DispatchBar` quick-create
(`handleDispatch`) already auto-opens the new session via `onOpenSession`, but
the New Session dialog (`handleCreateSubmit`) does not. Neither path does an
optimistic card insert, and neither shows the user's prompt before the echo.

## Goals

1. The new session **card appears instantly** in the dashboard list.
2. Creating a session **auto-opens** its conversation view (both create paths).
3. The **conversation thread is underway at creation time** (prompt fires on
   the immediate attach).
4. The user's **initial prompt is shown immediately** as the first message,
   before the agent's first event.

## Non-goals

- No new backend conversation/thread/message table. The transcript remains the
  Claude SDK `.jsonl` file reconstructed from the replayed event stream.
- No change to the deferred-prompt-on-attach mechanism.
- No eager server-side sending of the prompt before a consumer attaches.

## Design

All changes are **frontend**. No backend change.

### 1. Card appears instantly — `web/src/api/hooks.ts`

In `useCreateSession.onSuccess(session)`, write the returned `Session` directly
into the `QK.sessions` query cache via `setQueryData` (append if absent), then
still `invalidateQueries({ queryKey: QK.sessions })` so the next poll reconciles
authoritative server state. This removes the up-to-5s visibility lag without
relying on optimistic-before-response rollback (the server assigns the id and
account, so we insert on success, not on mutate).

### 2. Auto-open both create paths — `web/src/components/Dashboard.tsx`

- `handleCreateSubmit.onSuccess(session)`: after closing the dialog, call
  `onOpenSession(session.id, initialPrompt)` (newly accepts the prompt).
- `handleDispatch.onSuccess(session)`: already calls `onOpenSession(session.id)`;
  extend it to pass the `initialPrompt` too.

The `initialPrompt` passed is the trimmed text the user submitted (the same
value sent to the API).

### 3. Thread underway at creation — no code

Falls out of #2: auto-open mounts `SessionView`, whose mount effect calls
`attach(sessionId)`. The orchestrator pops `_pending_initial_prompt` and runs
the turn with the client watching. Live stream, no race.

### 4. Show the prompt immediately — `App.tsx`, `SessionView.tsx`, `session/reducer.ts`

**Thread the prompt through navigation.** `App`'s view state-machine gains an
optional `initialPrompt` on the `session` variant:

```ts
type View =
  | { type: 'dashboard' }
  | { type: 'session'; sessionId: string; originElementId?: string; initialPrompt?: string }
  | { type: 'review'; sessionId: string }
```

`handleOpenSession(sessionId, initialPrompt?)` sets it; `SessionView` receives
`initialPrompt?: string` and passes it down.

**Seed an optimistic user turn.** On mount (when `initialPrompt` is present),
`SessionView` dispatches a new local action `_seedUserPrompt`. The reducer adds
a `user`-role turn with a sentinel id (e.g. `optimistic:user`) and the prompt
text. The user's message is on screen before any WS event arrives.

**Adopt on echo (seed + replace).** When the SDK echoes the prompt back as a
`user` event (real server `turnId`), the reducer must replace the optimistic
turn rather than append a duplicate. Reconciliation rule:

- If an unadopted optimistic user turn exists whose text equals the incoming
  `user` event's text, **re-key** that optimistic turn to the event's `turnId`
  (adopt it) instead of appending a new turn.
- Fallback: if exactly one unadopted optimistic user turn exists and no
  text match is found (e.g. minor normalization differences), adopt it anyway —
  a brand-new session's first `user` event is unambiguously that prompt.

This rule lives in the reducer's existing `user` case (plus a small amount of
optimistic-turn bookkeeping), matching the established pattern of local
synthetic actions (`_resolvePermission`, `_resolveQuestion`).

## Edge cases

- **No initial prompt:** no seed; just instant card + auto-open. Conversation
  view shows its normal empty/idle state.
- **Text mismatch on echo:** handled by the single-unadopted-turn fallback.
- **Replay path:** a brand-new session has an empty transcript replay, so the
  seed cannot collide with replayed history. (Re-opening an existing session
  never carries an `initialPrompt`, so no seed occurs there.)
- **Back-then-reopen:** reopening from the dashboard passes no `initialPrompt`,
  so the thread renders purely from replay — no double seeding.

## Data flow (new session with prompt)

```
User submits New Session dialog
  → POST /api/sessions (201, returns Session; DB row already persisted)
  → useCreateSession.onSuccess:
        setQueryData(QK.sessions, append)   // card appears instantly
        invalidateQueries(QK.sessions)
  → handleCreateSubmit.onSuccess:
        onOpenSession(session.id, initialPrompt)
  → App: view = {session, sessionId, initialPrompt}
  → SessionView mounts:
        dispatch(_seedUserPrompt)           // prompt visible immediately
        attach(sessionId)                    // WS attach
  → orchestrator pops _pending_initial_prompt → driver.send_prompt
  → SDK runs turn, echoes `user` event (real turnId)
  → reducer adopts optimistic turn (re-key, no duplicate)
  → assistant events stream live
```

## Testing

- **Reducer (vitest):** `_seedUserPrompt` adds an optimistic user turn; a
  subsequent matching `user` event adopts it (no duplicate); a non-matching
  single-unadopted turn is adopted via fallback; multiple/zero optimistic turns
  behave correctly.
- **Hooks/Dashboard:** creating a session inserts the card into the cache
  immediately and navigates to the session view with the prompt threaded.
- **Regression:** existing `web/src/components/__tests__/KeyboardSessions.test.tsx`
  and the broader `vitest run` / `tsc --noEmit` / `eslint` gates stay green.

## Files touched

- `web/src/api/hooks.ts` — optimistic cache insert in `useCreateSession`.
- `web/src/components/Dashboard.tsx` — auto-open dialog path; pass prompt on both paths.
- `web/src/App.tsx` — `initialPrompt` on the `session` view variant + `handleOpenSession`.
- `web/src/components/session/SessionView.tsx` — accept `initialPrompt`, seed on mount.
- `web/src/session/reducer.ts` — `_seedUserPrompt` action + adoption rule in the `user` case.
- Tests alongside the above.
