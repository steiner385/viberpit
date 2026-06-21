# Session Prompt Image Input — Design

**Date:** 2026-06-21
**Status:** Approved (design) — pending implementation plan

## Goal

Let the user attach images to session prompts — by pasting from the clipboard,
dragging a file onto the prompt box, or picking a file — in **both** prompt
entry points (the in-session `Composer` and the new-session `DispatchBar`).
Claude must *see* the image directly (native multimodal input), and the image
must re-render when the session is reopened after a reload or server restart.

## Why native content blocks (not file-reference)

`ClaudeSDKClient.query()` accepts either a string **or** an async-iterable of
message dicts (`client.py:283`). The string path wraps text as
`{"type":"user","message":{"role":"user","content":<str>}}`. The iterable path
writes each dict straight to the CLI's stream-json stdin. So an image is sent as
one user message whose `content` is a content-block array — the standard
Anthropic shape the CLI accepts:

```json
{"type":"user","message":{"role":"user","content":[
  {"type":"text","text":"<prompt text>"},
  {"type":"image","source":{"type":"base64","media_type":"image/png","data":"<b64>"}}
]},"parent_tool_use_id":null,"session_id":"default"}
```

Rejected alternative:
- **Save to cwd + `[image: ./x.png]` text reference** — pollutes the worktree
  (trips the dirty-check on session close) and the model only sees the image if
  it chooses to call `Read`. Not faithful to "paste an image into the prompt".

## Transport: HTTP upload, not base64-over-WS (corrected from initial design)

The WS receive loop enforces a **256 KB frame cap** (`ws.py:48`,
`_MAX_FRAME = 262144`). A single 5 MB image base64-encodes to ~6.8 MB — 26× over
the cap — so carrying image bytes inside the WS `prompt` frame is infeasible, and
raising the cap to tens of MB is an unacceptable frame size even for a
single-user tool. Therefore image **bytes** travel over HTTP and the WS frame
carries only tiny `sha` references:

- `POST /api/uploads` (multipart, one image per request, behind `current_identity`)
  validates + saves the bytes content-addressed and returns `{sha, mime, name}`.
- The client renders the chip preview locally with `URL.createObjectURL(file)`
  (no download round-trip).
- The WS `prompt` frame and the create-session body carry only
  `images: [{sha, mime, name}]` — kilobytes, well under the cap, which we keep.

## Architecture & data flow

```
Composer / DispatchBar   (paste | drop | file-pick)
   │  per image:  POST /api/uploads (multipart)  ──►  {sha, mime, name}
   │  chip preview rendered locally via URL.createObjectURL(file)
   ▼  on send, carry only sha refs:
   Composer:    WS {type:'prompt', sessionId, text, images:[{sha,mime,name}]}
   DispatchBar: POST /api/sessions {initialPrompt, repo, images:[{sha,mime,name}]}
   ▼
bridge/ws.py / api/sessions.py → orchestrator.send_prompt(session_id, text, images)
   ▼
driver.send_prompt(text, images) → _start_turn:
   • associate: move <uploads_root>/_staging/<sha> → <uploads_root>/<session_id>/<sha>.<ext>
   • read bytes → base64 → image content block
   • emit ev.UserMessage(text, images=[{url,mime,name}])   (live render)
   • query(text)               when images is empty   (unchanged string path)
     query(aiter([user-msg]))  when images present     (text + image/base64 blocks)
```

- **`<uploads_root>`** = `~/.claude-accounts/uploads` (alongside `archive/`),
  overridable via `CCSWITCH_HOME`. `POST /api/uploads` saves to `_staging/<sha>.<ext>`;
  the driver moves it to `<session_id>/<sha>.<ext>` when the prompt is sent.
  Content-addressed by SHA-256 so identical pastes dedupe and filenames can't
  collide or path-traverse. Staging files unreferenced after 6 h are swept on
  startup (a paste that's never sent leaves no permanent litter).
- **Serving:** `GET /api/sessions/{id}/images/{sha}` behind `current_identity`.
  Live and replay both render via this stable URL.

## Limits & validation

Enforced on **both** client and server (client for instant feedback, server as
the authority):

| Rule | Value |
|------|-------|
| Allowed MIME types | `image/png`, `image/jpeg`, `image/gif`, `image/webp` |
| Max size per image | 5 MB (pre-base64) |
| Max images per message | 8 |

Image-only messages (no text) are allowed — the Anthropic API permits a content
array with only image blocks.

## Persistence & replay (the one open risk)

There is no ccswitch event store: live events stream over WS and are not saved.
The only persistence is the Claude CLI's own
`<config_dir>/projects/*/<claude_session_id>.jsonl`, which `replay.py` re-parses
on reattach. Today `replay.py` rebuilds a user turn from **text blocks only**
and drops every other block type.

Whether replay shows images hinges on one unknown: **does the CLI persist the
pasted image inline in its JSONL?**

- **Plan Task 1 is a spike** — a ~15-line standalone script that sends a 1×1 PNG
  through the SDK and dumps the resulting JSONL user line.
- **If inline (expected):** add an `image`-block branch to `replay.py` that emits
  the same `UserMessage.images` shape. Perfect correlation (same line as the
  user text), zero new storage.
- **If not:** append a per-session `<uploads_root>/<session_id>/turns.jsonl`
  sidecar (`{ts, text, images:[{sha,mime,name}]}` per user message) and, on
  replay, take user turns from the sidecar while taking assistant turns +
  tool-results from the CLI JSONL, interleaving by timestamp.

Live rendering does not depend on this: the driver already synthesizes its own
`ev.UserMessage` (the SDK never echoes the human prompt back), so we just add
image refs to that event.

## Interfaces

- `ImageRef = {"sha": str, "mime": str, "name": str}` — the small reference shape
  carried by the WS `prompt` frame and the create-session body.
- `ev.UserMessage` gains `images: list[dict] | None`; `to_wire()` emits an
  `"images"` key (list of `{url, mime, name}`, where `url` =
  `/api/sessions/{id}/images/{sha}`).
- `driver.send_prompt(text: str, images: list[ImageRef] | None = None)`. The
  queued-prompt path (`self._queue`) carries `(text, images)` tuples so a queued
  prompt keeps its images.
- `orchestrator.send_prompt(session_id, text, images=None)`.
- WS `prompt` message gains an optional `images` array of `ImageRef`.
- `POST /api/sessions` body gains an optional `images` array of `ImageRef`,
  stored alongside the deferred `initialPrompt` and sent on first attach.
- New `server/sessions/uploads.py`:
  - `stage_image(mime: str, raw: bytes) -> ImageRef` — validates + writes
    `_staging/<sha>.<ext>`.
  - `associate(session_id: str, ref: ImageRef) -> Path` — moves staging → session
    dir (idempotent; tolerates an already-associated sha).
  - `image_path(session_id: str, sha: str) -> Path | None` — for the serving route.
  - `read_b64(path: Path) -> str` and `sweep_staging(max_age_h: float = 6)`.

## Components touched

- **Frontend:** `web/src/components/session/Composer.tsx` and
  `web/src/components/DispatchBar.tsx` — paste/drop/file-pick handlers, thumbnail
  chip strip with remove buttons, oversize/wrong-type inline errors. A shared
  `useImageAttachments` hook (validates, uploads to `POST /api/uploads`, holds
  `{sha,mime,name,previewUrl}[]`) + an `AttachmentChips` component so both inputs
  reuse one implementation (single source of truth).
  `web/src/ws/client.ts` — `prompt(sessionId, text, images?)`.
- **Backend:** new `server/sessions/uploads.py`; `POST /api/uploads` and the
  `GET /api/sessions/{id}/images/{sha}` serving route in `server/api/sessions.py`
  (create-session body gains `images`); `server/bridge/ws.py`,
  `server/sessions/orchestrator.py`, `server/sessions/driver.py`,
  `server/sessions/events.py`, `server/sessions/replay.py`.

## Error handling

- Unsupported type or oversize → client blocks the attach and shows a chip-level
  error; nothing is uploaded.
- `POST /api/uploads` independently validates type (content sniff, not just the
  declared MIME) and size → `400` for a bad image; the client surfaces the error
  on the chip and drops it.
- A `sha` in the `prompt`/create body with no staged or associated file → that
  image is skipped, the text is still sent, and a `warning` event is surfaced;
  the turn is never failed by a missing attachment.
- Associate/move failure → logged, image skipped, text proceeds (non-fatal, same
  posture as `sync_back`).

## Testing

- **Driver:** images present → `query` is called with the content-block iterable
  (assert the yielded dict shape); images empty → unchanged string path.
- **Replay:** an `image` block in a fixture JSONL → `UserMessage.images`
  populated (or the sidecar path, per the spike outcome).
- **WS / orchestrator:** images thread through `send_prompt`; queued prompt keeps
  its images.
- **Uploads:** `save_image` is content-addressed + dedupes; the serving route
  requires identity and 404s an unknown sha; validation rejects bad type/size.
- **Frontend:** Composer + DispatchBar paste event, drop, file input, oversize
  rejection, chip removal, and that a text-only send is unaffected.

## Out of scope (YAGNI)

- Non-image attachments (PDF/docs).
- Images larger than 5 MB.
- Drag-reordering attachment chips.
