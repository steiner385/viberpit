# Image Attachments — Unify the Two Parallel Implementations

**Date:** 2026-06-22
**Status:** Approved (design) — pending spec review
**Related:** PR #94 (`feat(attachments)`), master PR #31 (`feature/prompt-image-input`)

## Problem

Two complete, parallel implementations of the *same* end-user feature exist:

- **Master (shipped, PR #31):** `server/sessions/uploads.py` + `/api/uploads` + `useImageAttachments`/`AttachmentChips`. Content-addressed (sha256) filesystem storage, strict MIME validation, no DB. Dispatch payload: `images: {sha,mime,name}[]`.
- **PR #94 (open):** `server/sessions/attachments.py` + `Attachment` DB model + `/api/attachments` + `useAttachments`/`AttachmentTray`. DB-persisted with session/turn binding, orphan sweep, audit trail, streaming upload. Dispatch payload: `attachmentIds: string[]`.

Keeping both violates the project constitution (Principle VI: single source of truth / no dead code): duplicate endpoints, hooks, reducer paths, and storage modules. Exactly one implementation must remain.

## Goal

A **single canonical** image-attachment feature that combines the strongest parts of each:

- DB-backed persistence + lifecycle (from #94) — the genuine superset capability.
- Content-addressed storage + dedup + strict MIME validation (from master).
- The web UI already shipping in production (master's `useImageAttachments` / `AttachmentChips`).

Non-goals: changing the wire format of image blocks sent to Claude (both already emit the same Anthropic `image`/`base64` block shape); any new UX beyond what exists.

## Decisions (confirmed)

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Persistence | **DB `Attachment` model** (#94) | audit, lifecycle, queryable, session/turn binding |
| Storage layout | **content-addressed by sha256** (master) | dedup; same image uploaded twice → one file |
| Dedup | **on by sha256** | a re-upload of identical bytes reuses the existing row + file |
| MIME validation | **sniff + reject when declared ≠ sniffed** (master) | type-confusion defense |
| Upload I/O | **streaming with hard byte cap** (#94) | memory-bounded |
| Limits | **10 MB / 5 images per message** (#94) | per user |
| API surface | **`/api/attachments`** (DB-backed); delete `/api/uploads` + `/images/{sha}` | one surface |
| Web hook/component | **`useImageAttachments` + `AttachmentChips`** (master); delete `useAttachments`/`AttachmentTray` | already live in prod |
| Dispatch payload | **`attachmentIds: string[]`** | natural for DB-backed rows; replaces master's `images:[{sha}]` |
| Timestamps | **timezone-aware UTC** | fix #94's naive `datetime.now()` |
| Prod migration | **Alembic migration for new `attachments` table** | dev SQLite `create_all` covers dev; prod/Postgres needs it (project rule) |

## Architecture

### Server

- **`server/sessions/attachments.py`** (canonical): `Attachment` model helpers + pure helpers.
  - `sniff_media_type(head)` — keep #94's; add master's declared-vs-sniffed reconciliation at the API layer.
  - Storage: write file at `attachments_dir()/{sha256}.{ext}` (content-addressed), **not** `{uuid}.{ext}`. The DB row keeps its own `id` (UUID) as the API handle, plus `sha256` for dedup and the on-disk filename.
  - `resolve_and_bind(db, session_id, turn_id, ids)` — bind rows to a session/turn, return descriptors with the content-addressed `path`.
  - `sweep_orphans(db, older_than)` + `delete_for_session(db, session_id)` — keep #94's lifecycle. File unlink must be **dedup-aware**: only unlink the on-disk sha file when no other `Attachment` row references that sha.
- **`server/api/attachments.py`** (canonical): `POST /api/attachments` (streaming, cap, sniff, dedup-by-sha, create row, audit) and `GET /api/attachments/{id}` (serve by row → sha file).
- **Delete** `server/sessions/uploads.py`, the `uploads_router`, its `/images/{sha}` route, and master's startup `sweep_staging()` call. Replace the startup/background sweep with #94's `sweep_orphans` loop.
- **`Attachment` model** (`server/db/models.py`): keep #94's columns; fix `created_at` default to timezone-aware UTC. Add an **Alembic migration** creating the `attachments` table.
- Re-thread the send path (`bridge/ws.py`, `sessions/driver.py`, `sessions/orchestrator.py`, `sessions/events.py`, `sessions/replay.py`, `api/sessions.py`): the prompt-send accepts `attachmentIds`, binds via `resolve_and_bind`, loads each file, emits the Anthropic image block, and persists refs for replay rendering.

### Web

- **Keep** `useImageAttachments` + `AttachmentChips`; **delete** `useAttachments` + `AttachmentTray`.
- Rewire the hook to call `POST /api/attachments` and track returned `{id, mediaType, filename, byteSize, url}`. The dispatch/prompt payload carries `attachmentIds: string[]` (replacing master's `images:[{sha,mime,name}]`).
- Transcript rendering (`Turn`/`ToolCallCard`/reducer image block): render image refs via `GET /api/attachments/{id}` on both live send and replay.
- Update `api/hooks.ts`, `Dashboard.tsx`, `DispatchBar.tsx`, `Composer.tsx`, `SessionView.tsx`, `ws/client.ts`, and reducer image-block handling to the single `attachmentIds` shape.

## Data flow (send)

1. User attaches image → `useImageAttachments` uploads to `POST /api/attachments` → row created (sha-deduped), returns `{id, url}`.
2. UI shows `AttachmentChips`; dispatch payload includes `attachmentIds`.
3. On send, server `resolve_and_bind(session_id, turn_id, attachmentIds)` → binds rows, reads sha files → emits Anthropic image blocks to the SDK.
4. Refs persisted; replay renders via `GET /api/attachments/{id}`.

## Lifecycle

- **Orphans:** unbound rows (`session_id IS NULL`) older than cutoff swept by background loop (`sweep_orphans`).
- **Session delete:** `delete_for_session` removes rows; file unlink is dedup-aware (only when sha unreferenced).

## Error handling

- Upload: 413 over cap; 400 unsupported/sniff-fail; declared≠sniffed rejected.
- Bind: skip unknown ids (cap at 5); serve 404 for missing row/file.

## Testing

- Server: upload (happy, oversize, bad-type, declared≠sniffed mismatch), **dedup** (same bytes twice → one file/row reused), bind+cap, `sweep_orphans`, `delete_for_session` dedup-aware unlink, serve, audit row written. Migration applies cleanly.
- Web: `useImageAttachments` upload→chips→`attachmentIds` dispatch; transcript renders image refs (live + replay).
- All existing gates: tsc, eslint, vitest, ruff, mypy, pytest, **and `npm run build`** (the strict `tsc -b` that the standard typecheck misses).

## Migration / rollout

- This removes master's `/api/uploads` + `/images/{sha}`. No persisted production data depends on the old filesystem-only layout beyond transient staged files; the orphan sweep handles cleanup. No data migration required (the `attachments` table is new).

## Risks

- **Dedup unlink correctness:** deleting a session must not unlink a sha file still referenced by another session's row. Covered by a dedicated test.
- **Re-threading surface is large** (~17 conflicted files + the deletions). Land as one focused PR; verify the full gate set including `npm run build`.
