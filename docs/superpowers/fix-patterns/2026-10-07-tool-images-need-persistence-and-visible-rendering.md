# Tool images need persistence and visible rendering

Tracking: #2810. Source session: `cf0a257c-8814-4243-b493-d205189646dc`.
Feedback: `fe02cd2c-c911-4970-83a9-107a7ea9a1e6`.

## Symptom

The model could read screenshot results, but the user saw no inline images. The
source session's actual transcript API response contained four PNG `Read` calls
followed by successful `toolResult` events with empty payloads. Assistant Markdown
also used absolute workspace paths that the browser could not serve.

## Anti-pattern

Treating a native image item as a status-only tool result discards its deliverable.
Even preserved images disappear when rendered inside automatically collapsed tool
details. A host path in Markdown is not an attachment URL.

## Fix shape

Normalize supported native images, materialize them through the canonical
attachment store before emitting or recording events, and replay stable opaque
attachment URLs. Native image paths and data URLs stay inside the adapter; the
existing authenticated attachment endpoint serves bytes. Reuse the existing row
on replay so the original file can be removed without losing the image. Reject
remote sources without fetching them, and bound reads using attachment limits.

Render image content outside tool details. Use one image renderer for tool
results, shared artifacts, and assistant Markdown, including unavailable-image
feedback, responsive bounds, open and attachment download. Preserve enclosing
Markdown links. Workspace Markdown provides share_artifact guidance without a
filesystem-serving endpoint.

## Where to look

- `server/codex/schema.py`, `translator.py`, `images.py`, `driver.py`, `replay.py`.
- `server/sessions/attachments.py` and `tools/tools.py` for shared persistence.
- `web/src/features/sessions/transcript/ToolCallCard.tsx`, `ResultImages.tsx`,
  `MarkdownDoc.tsx`, and `ArtifactCard.tsx`.

The sibling audit found MCP result images and dynamic tool inputImage content
were also dropped; both now use the same materializer as imageView. The installed
Codex app-server schema generator confirmed all three fields. FunctionCallOutput
has no image field in that schema. ToolGroup has no additional collapsing layer;
UserBubble's upload-specific layout does not have the assistant tool collapse bug.

## Verification

Regressions failed first for empty native results and hidden tool images. Backend
tests cover source deletion, repeated/concurrent replay, live driver emission and
its durable turn snapshot, malformed/remote sources, and denied attachment access.
Browser coverage in `web/e2e/session-images.spec.ts` exercises live WebSocket frames,
reload/replay, open/download, missing/malformed images, and workspace Markdown at
390px and 1440px. Local browser verification used the existing Calendar screenshot
read-only via `VIBERPIT_IMAGE_FIXTURE`; CI defaults to the repository's PNG asset.
These browser tests use protocol/API fixtures; they do not deploy the branch or
change the source session. A missing original image from an older session cannot
be recovered unless it was previously materialized.
