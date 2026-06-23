# Persist queued messages across navigation + image support

**Date:** 2026-06-23
**Status:** Approved

## Problem

Two distinct pieces of "message queue" state are lost or invisible when the user
navigates away from a session and back:

1. **Submitted queue (the chip strip).** Prompts sent to the server while a turn is
   already running are appended to the server-side FIFO in `driver.py`
   (`self._queue: list[tuple[str, list[dict]]]`). That queue *does* survive
   navigation (the driver outlives the WebSocket attach), but:
   - On reattach, `_emit_live_extras` (`server/bridge/ws.py`) re-emits only
     `Status(count=N)` — never a `QueueUpdate` carrying the items — so the chip
     strip renders empty even though the queued prompts will still run.
   - `QueueUpdate.items` is `list[str]` (`server/sessions/events.py`), so the image
     half of each `(text, images)` tuple is dropped on the wire and queued
     attachments are never shown.

2. **Unsent draft.** The composer's `text` (`useState`) and uploaded-but-unsent
   attachments (`useImageAttachments`) are React component state. The runtime
   remounts per session (`key={selection.id}` on desktop, route change on mobile),
   destroying the draft on navigation.

## Goals

- Submitted queue chips repopulate (with image thumbnails) when navigating back to a
  session that has queued prompts.
- `QueueUpdate` carries per-item images so chips can render thumbnails.
- The unsent composer draft (text + uploaded attachments) survives navigation,
  per-device, via localStorage.

## Non-goals (YAGNI)

- Cross-device draft sync (localStorage is per-device by design).
- Garbage collection of attachments uploaded for a draft that is never sent.
- Reordering the queue.

## Design

### Part A — Submitted queue survives navigation + shows images

The driver already stores `url`/`mime`/`name` per queued image
(`driver.py` `_start_turn` builds exactly these wire refs), so no new data is needed —
only the wire format and the attach re-emit change.

- **`server/sessions/events.py`** — `QueueUpdate.items` becomes `list[dict]`, each item
  `{"text": str, "images": [{"url": str, "mime": str, "name": str}]}`. `to_wire()`
  passes the structured items through.
- **`server/sessions/driver.py`** — add a private `_queue_wire()` that maps
  `self._queue` to the structured item list (`name` defaults to `""`). Replace the four
  `[t for t, _ in self._queue]` call sites (append in `send_prompt`, drain-pop in
  `_drain`, `cancel_queued`, stop-clear in `stop`) with it. Add a public `queue_wire()`
  for the attach path.
- **`server/bridge/ws.py`** — in `_emit_live_extras`, after the existing `Status`
  emit, also emit `QueueUpdate(driver.queue_wire())` so a reattach repopulates the
  chips. `cancel_queued(index)` semantics are unchanged (index still addresses the same
  FIFO position).
- **`web/src/session/events.ts`** — `EvQueueUpdate.items: QueuedItem[]` where
  `QueuedItem = { text: string; images: { url: string; mime: string; name: string }[] }`.
- **`web/src/session/reducer.ts`** — `TranscriptState.queued: QueuedItem[]`; the
  `queueUpdate` case stores `ev.items` unchanged.
- **`web/src/components/session/Composer.tsx`** — `queued?: QueuedItem[]`; the queued
  chip strip renders `item.text` plus small `<img src={img.url}>` thumbnails, reusing the
  thumbnail styling from `AttachmentChips`. Cancel-by-index is unchanged.

### Part B — Unsent draft survives navigation (localStorage)

- **New hook `web/src/components/session/useSessionDraft.ts`** —
  `useSessionDraft(sessionId)` reads/writes
  `localStorage["ccswitch:draft:<sessionId>"]` holding
  `{ text: string, attachments: { id: string; mime: string; name: string }[] }`.
  Exposes the initial draft (read once on mount) plus `save(draft)` and `clear()`.
  Writing an empty draft (no text, no attachments) removes the key.
- **`web/src/components/session/useImageAttachments.ts`** — accept an optional
  `initial?: Attachment[]` seed. Rehydrated attachments use the durable server URL
  `/api/attachments/<id>` as `previewUrl` (the `URL.createObjectURL` blob URL cannot
  survive a reload; the server URL can).
- **`web/src/components/session/Composer.tsx`** — accept `sessionId`. On mount, hydrate
  `text` and attachments from the draft. On every change to text or attachments, persist.
  On submit/queue, clear the draft. This is orthogonal to the existing in-memory
  history-recall `draftRef` (a different concept).
- **`web/src/components/session/SessionComposer.tsx`** — pass `sessionId={rt.sessionId}`.

## Testing

**Backend (pytest, `server/tests/`):**
- `QueueUpdate.to_wire()` includes structured items with images.
- Driver emits structured items on append, cancel, and drain-pop.
- WS attach (`_emit_live_extras`) re-emits the full queue with images.

**Frontend (vitest, `web/`):**
- Reducer maps a structured `queueUpdate` into `state.queued`.
- Composer renders queued-item text and image thumbnails.
- `useSessionDraft` round-trips text + attachments and clears on submit.
- Rehydrated attachments use `/api/attachments/<id>` as the preview URL.

## Affected files

Backend: `server/sessions/events.py`, `server/sessions/driver.py`,
`server/bridge/ws.py`.
Frontend: `web/src/session/events.ts`, `web/src/session/reducer.ts`,
`web/src/components/session/Composer.tsx`,
`web/src/components/session/SessionComposer.tsx`,
`web/src/components/session/useImageAttachments.ts`,
`web/src/components/session/useSessionDraft.ts` (new).
