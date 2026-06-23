# Mobile camera capture for prompt image attachments

**Date:** 2026-06-23
**Status:** Approved

## Goal

On a mobile device, the prompt composer's attach button should offer the native
"Take Photo / Choose from Library" sheet (on both iOS and Android), and a photo
captured on an iPhone (HEIC) should upload successfully — not just pre-saved
gallery images.

## Background (verified against the code, not assumed)

Image attachment is single-source-of-truth on the frontend:

- The mobile (`SessionView`) and desktop layouts mount the **same**
  `web/src/components/session/Composer.tsx`.
- New-session creation uses `web/src/components/DispatchBar.tsx`.
- Both feed `web/src/components/session/useImageAttachments.ts` → `addFiles()`,
  which validates against `ALLOWED = ['image/png','image/jpeg','image/gif','image/webp']`,
  `MAX_BYTES = 10 MB`, `MAX_IMAGES = 5`, then POSTs each file to `/api/attachments`.

Both inputs currently use `accept="image/png,image/jpeg,image/gif,image/webp" multiple`
with **no** `capture` attribute.

Backend (verified):

- `server/api/attachments.py` validates uploads by **magic-byte sniffing**
  (`server/sessions/attachments.py`), not the declared `Content-Type`, and rejects
  declared-vs-sniffed mismatches as a type-confusion defense. Allowed by bytes:
  png / jpeg / webp / gif.
- The driver base64-encodes each stored image and sends
  `{"type":"image","source":{"type":"base64","media_type":...}}` to the
  claude-agent-sdk. **Claude only accepts png/jpeg/webp/gif media types.**

### Key design consequence

"HEIC support end-to-end" **cannot** live on the server: Claude itself cannot
consume HEIC, so accepting HEIC server-side would only let an unusable file
through that fails downstream at the model. The correct realization is
**client-side HEIC→JPEG conversion before upload**. The server stays unchanged
(already correct and secure), and Claude receives a JPEG it understands.

## Design

### 1. Trigger the native camera sheet (no `capture` attribute)

Change the two hidden file inputs' `accept` from the specific MIME list to
`accept="image/*"`, keeping `multiple`. This makes Android Chrome reliably show
the camera option (iOS already does) while preserving library/files selection.
We deliberately do **not** add `capture`, which would force camera-only and
remove the library option.

- `web/src/components/session/Composer.tsx`
- `web/src/components/DispatchBar.tsx`

Non-image / unsupported picks under `image/*` (svg, bmp, tiff, …) are still
rejected gracefully by the existing client validation and the server's
magic-byte sniff.

### 2. Client-side HEIC→JPEG conversion

New helper `web/src/components/session/heicConvert.ts`:

```ts
export async function maybeConvertHeic(file: File): Promise<File>
```

- Detects HEIC/HEIF by `file.type` (`image/heic`, `image/heif`) **and** by
  filename extension (`.heic`, `.heif`) — HEIC files frequently arrive with an
  empty `file.type`.
- Lazy-imports `heic2any` via dynamic `import()` so the libheif wasm payload
  stays out of the main bundle; non-HEIC users pay zero bundle cost.
- Converts to JPEG and returns a new `File` named `*.jpg` with type
  `image/jpeg`. Non-HEIC files pass through untouched. Conversion failure throws.

Wire into `useImageAttachments.addFiles()`:

- Run `maybeConvertHeic(file)` **before** the existing `ALLOWED` / size checks,
  so the converted JPEG flows through the unchanged validation + `/api/attachments`
  path.
- Re-check size **after** conversion (a converted JPEG can differ in size).
- On conversion failure, push an error chip (e.g. "Couldn't read photo") using
  the existing error-chip mechanism.
- `ALLOWED`, `MAX_BYTES`, `MAX_IMAGES` are unchanged. `previewUrl` is created
  from the (possibly converted) file so the thumbnail renders correctly.

### 3. Dependency

Add `heic2any` to `web/package.json`, loaded lazily (dynamic import only).

### 4. Server

No changes. Verified correct and secure as-is.

### 5. Tests (TDD, vitest)

`heicConvert` unit tests (heic2any mocked):
- HEIC by `file.type` → converts to a `image/jpeg` `.jpg` File.
- HEIC by extension with empty `file.type` → converts.
- Non-HEIC (png/jpeg) → returned unchanged (heic2any not called).
- heic2any rejects → `maybeConvertHeic` rejects.

`useImageAttachments` tests:
- A HEIC file ends up uploaded as JPEG (fetch receives a jpeg File; resulting
  attachment has no error).
- Conversion failure produces an error chip and does not POST.

## Out of scope (YAGNI)

- Server-side HEIC decode / accepting HEIC in the server allow-list.
- Video capture.
- A separate camera button or a "Take Photo / Choose Library" custom menu — the
  native sheet already provides both.
- EXIF-orientation handling beyond whatever heic2any emits.
