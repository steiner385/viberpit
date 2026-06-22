# Inline Image Attachments — Design

**Date:** 2026-06-22
**Feature:** Attach images to prompts; display them inline in chat and send them to Claude.
**Status:** Approved design, pending spec review.

## Summary

Users can attach one or more images to a prompt — in the live session composer **and** in
the new-session dispatch bar. Each attached image is:

1. **Displayed inline** in the chat transcript (a thumbnail grid on the user's message bubble),
   surviving page reloads.
2. **Sent to Claude** as a real vision input (a base64 image content block), so the model
   actually sees the image.

Images travel browser → backend over a dedicated **REST multipart upload** (not the WebSocket,
which has a 256 KB inbound frame cap). The upload stores the file on disk and returns an opaque
**attachment ID**. Prompts then carry only the small IDs. The backend reads the file from disk to
build the base64 block for the SDK, and the UI renders thumbnails via an identity-gated serve
endpoint.

## Goals / Non-goals

**Goals**
- Attach via paste, file picker, and drag-and-drop.
- Available in both the live composer and the new-session dispatch bar.
- Image is both displayed and sent to Claude.
- Inline display persists across reload (transcript replay reconstructs it).

**Non-goals (v1)**
- Non-image attachments (PDF, video, audio).
- Animated-GIF playback fidelity (GIF accepted as a still image, per vision support).
- Editing/annotating images in-app.
- Image output *from* Claude (assistant-produced images) — out of scope; this is user→model only.

## Constraints discovered

- **SDK send path:** `ClaudeSDKClient.query()` accepts `str | AsyncIterable[dict]`. The string
  path sends `{"role":"user","content":"<text>"}`; images require the async-iterable path with
  `content` as a **list of content blocks** including `{"type":"image","source":{"type":"base64",…}}`.
- **256 KB WS frame cap** (`server/bridge/ws.py` `_MAX_FRAME`) — rules out base64-over-WebSocket;
  drives the dedicated REST upload.
- **DB rules** (CLAUDE.md): `create_all` *creates missing tables* (fine for a new table); the
  nullable-column backfill caveat only applies to ALTERing existing tables, so new columns on
  existing tables must be nullable. The new `attachments` table is unaffected.
- **Constitution:** Principle III (verify against the real binary, not assumptions) gates the
  replay design — see §6.

---

## 1. Data model — new `Attachment` table

Added to `server/db/models.py` (declarative `Base`, `Mapped`/`mapped_column`, `_new_uuid` default).

| column        | type                 | notes                                                                 |
|---------------|----------------------|-----------------------------------------------------------------------|
| `id`          | str PK               | uuid hex; on-disk filename stem and serve handle                      |
| `session_id`  | str, nullable, index | **null at upload** (new-session uploads precede the session); bound when the prompt is sent |
| `turn_id`     | str, nullable        | our `user_turn` id, set at send time                                  |
| `media_type`  | str                  | sniffed, e.g. `image/png`                                             |
| `filename`    | str                  | original name, sanitized — used as `alt` text                         |
| `byte_size`   | int                  |                                                                       |
| `sha256`      | str, index           | content hash — replay join key (see §6)                               |
| `uploaded_by` | str                  | identity, for audit                                                   |
| `created_at`  | datetime             | drives orphan cleanup                                                 |

**Disk layout:** flat, keyed by id — `<root>/attachments/<id>.<ext>`, where `<root>` is
`ccswitch.paths.root()` (`~/.claude-accounts`, env-overridable via `CCSWITCH_HOME`). Add a
`ccswitch.paths.attachments_dir()` helper (`root() / "attachments"`, created `0o700`).
Flat-by-id because uploads happen before a session exists; the DB row holds the session linkage.

## 2. Upload endpoint — `POST /api/attachments`

New router `server/api/attachments.py`, identity-gated via `current_identity`.

- Body: `multipart/form-data`, field `file`, optional `sessionId`.
- **Validation (server re-validates; never trust the client):**
  - Sniff magic bytes → allow only PNG, JPEG, WebP, GIF. Reject mismatched/unknown → 400.
  - Enforce ≤ 10 MB during a **capped streaming read** (stop + 413 once the cap is exceeded;
    don't buffer the whole body first).
- Compute `sha256`, write to `<root>/attachments/<id>.<ext>`, insert the row
  (`session_id` from body or null, `uploaded_by`=identity).
- Audit: `attachment.upload`.
- Response `201`: `{ id, mediaType, filename, byteSize, url: "/api/attachments/<id>" }`.

Per-message **count** (≤ 5) is enforced at send time (see §5), since uploads are one file each.

## 3. Serve endpoint — `GET /api/attachments/{id}`

- Identity-gated. Look up the row; stream the file with the stored `Content-Type`,
  `Cache-Control: private`, `Content-Disposition: inline`. 404 if the row/file is missing.
- IDs are unguessable UUIDs; any verified identity may fetch (consistent with the existing
  identity-gated transcript endpoint — not per-user scoped).

## 4. Wire-protocol changes

- **WS `prompt` frame** (`server/bridge/ws.py`): add optional `attachmentIds: string[]`.
  `orchestrator.send_prompt(session_id, text, attachment_ids)`.
- **`CreateSessionRequest`** (`server/api/sessions.py`): add `attachmentIds: list[str] | None`,
  carried alongside the deferred `initialPrompt` and replayed into `send_prompt` on first attach
  (`pop_pending_initial_prompt` path).
- **`EvUser` / `UserMessage`**: add `attachments: [{id, mediaType, filename}]`.
  - `server/sessions/events.py` `UserMessage` dataclass + `to_wire`.
  - `web/src/session/events.ts` `EvUser`.
- **Driver queue:** the FIFO element changes `str → {text, attachments}` (small dataclass
  `QueuedPrompt`). `QueueUpdate` still ships text previews, plus an image count for the strip.

## 5. Backend send path (driver + SDK)

- `orchestrator.send_prompt(id, text, attachment_ids)`:
  - Resolve `Attachment` rows by id (scoped to this session or still-orphan); **bind** them
    (set `session_id` + `turn_id`); ignore unknown ids; enforce ≤ 5 (reject/trim extras with a
    surfaced error).
  - Hand the driver resolved descriptors `{path, media_type, id, filename}`.
- `SDKClientProtocol.query` signature widens `str → str | AsyncIterable[dict]`;
  `FakeSDKClient` (tests) updated to accept and record both shapes.
- `SessionDriver._start_turn(text, attachments)`:
  - **No attachments:** unchanged string path (`query(text)`).
  - **With attachments:** send via a one-item async iterable yielding
    `{"type":"user","message":{"role":"user","content":[{"type":"text","text":text}, {"type":"image","source":{"type":"base64","media_type":mt,"data":b64}} …]}}`.
    Base64 is read off-thread (`asyncio.to_thread`) to avoid blocking the event loop.
  - Emit `ev.UserMessage(turn_id=user_turn, text=text, attachments=[refs])` for the live echo.
- Auto-title still derives from `text` (an image-only prompt with empty text falls back to a
  generic title, e.g. "Image prompt").

## 6. Replay / reload persistence

A reloaded page reattaches and `replay.py` rebuilds turns from the `claude` transcript JSONL.

- **Primary approach:** detect `type:image` blocks on user turns in the transcript; decode →
  SHA-256 → look up the `Attachment` row → emit an attachment ref so the UI serves the clean
  `/api/attachments/{id}` URL (no base64 re-ship).
- ⚠️ **Principle III verification (plan task, before finalizing this file):** confirm how the
  real `claude` CLI persists image content blocks in its transcript JSONL — inline base64 vs.
  externalized reference. If it does **not** store inline base64, fall back to an **ownership
  join**: reconstruct from our own `Attachment` store in send-order per session (the rows carry
  `session_id`, `turn_id`, `created_at`). The store is the source of truth either way; only the
  *join mechanism* depends on the verification outcome.

## 7. Frontend

- **API:** `web/src/api/` — `uploadAttachment(file): Promise<{id, mediaType, filename, byteSize, url}>`
  (multipart POST, same auth as existing calls).
- **Shared attach UI:** a `useAttachments` hook + an `AttachmentTray` component (thumbnails with
  ✕ remove), reused by both `Composer` and `DispatchBar`:
  - Inputs: **paste** (`onPaste` image items), **drag-drop** (`onDrop`), **file picker**
    (📎 button + hidden `<input type=file accept=image/* multiple>`; mobile gets camera/library).
  - Client-side validation (type/size/count) with inline errors; upload-on-add with a per-thumb
    spinner; tracks `attachmentIds`; blocks submit while uploads are in flight.
- **Composer:** `onPrompt(text, attachmentIds)`; clear tray on submit. Allow image-only sends.
- **DispatchBar:** `onDispatch({ initialPrompt, repo, attachmentIds })`; new-session uploads have
  no `sessionId` yet — bound at session creation.
- **WS/create wiring:** the prompt sender adds `attachmentIds`; `POST /api/sessions` includes them.
- **Render:** reducer (`web/src/session/reducer.ts`) gains an `image` block on the user turn;
  `UserBubble` renders the text bubble plus a responsive thumbnail grid
  (`<img src="/api/attachments/{id}" alt={filename}>`, click to expand/lightbox).

## 8. Lifecycle & cleanup

- **Permanent delete** of a session (`orchestrator.permanent_delete`) → unlink the session's
  attachment files and delete their rows.
- **Archive** keeps attachments (the transcript stays viewable).
- **Orphan sweep:** periodically delete `session_id IS NULL` rows (and files) older than 24 h
  (uploaded but never sent). Lightweight background task on the app lifespan.

## 9. Testing (TDD — Principle V)

**Server (`server/tests/`)**
- Upload: magic-byte sniff accepts PNG/JPEG/WebP/GIF, rejects spoofed Content-Type / non-images;
  size cap returns 413; happy path returns `201` + descriptor and writes the file + row.
- Serve: identity-gated (401 without identity), correct Content-Type, 404 for unknown id.
- Send path: with attachments, `FakeSDKClient` receives a structured message with the expected
  text + image blocks; without attachments, the string path is preserved.
- Queue carries attachments across turns; `QueueUpdate` reports image counts.
- Replay reconstructs attachment refs (per the §6 verified mechanism).
- Permanent-delete unlinks files + rows; orphan sweep removes stale null-session rows.

**Web (`web/`, vitest)**
- `useAttachments`: paste/drop/pick add; type/size/count validation + errors; remove.
- `Composer` and `DispatchBar` forward `attachmentIds`.
- Reducer adds the image block on the user turn.
- `UserBubble` renders text + thumbnail grid with correct `src`/`alt`.

## 10. Files touched (summary)

- **New:** `server/api/attachments.py`, `Attachment` model in `server/db/models.py`,
  `ccswitch.paths.attachments_dir()`, `web/src/api/uploadAttachment`,
  `web/src/components/session/AttachmentTray.tsx`, `web/src/components/session/useAttachments.ts`.
- **Changed:** `server/bridge/ws.py`, `server/api/sessions.py`, `server/sessions/orchestrator.py`,
  `server/sessions/driver.py`, `server/sessions/sdk_contract.py`, `server/sessions/sdk_factory.py`
  (fake), `server/sessions/events.py`, `server/sessions/replay.py`,
  `web/src/components/session/Composer.tsx`, `web/src/components/DispatchBar.tsx`,
  `web/src/components/session/UserBubble.tsx`, `web/src/session/reducer.ts`,
  `web/src/session/events.ts`, plus the WS client and app registration for the new router.
