# Bulk archived-session delete: live progress + cancel

**Date:** 2026-06-30
**Scope:** frontend only (`web/`). No backend, API, or DB changes.

## Problem

Deleting many archived sessions at once (observed: 95) gives misleading, opaque feedback:

1. **"Permanently delete 0 sessions"** — the confirm dialog's count is derived live from the
   `QK.archived` query cache (`selectedNames = archivedSessions.filter(s => selected.has(s.id))`).
   On confirm, `useBulkPermanentDeleteSessions`'s `onMutate` optimistically removes *all* targeted
   IDs from that cache up front, so while the dialog is still mounted (`isPending`) its count
   recomputes to 0.
2. **No progress** — the backend `POST /sessions/batch/permanent-delete` processes every ID in one
   sequential request (`shutil.rmtree` + archive-live + DB delete per session) and only responds
   when all are done. For 95 sessions that is minutes of a static "Deleting…" with no signal that
   work is happening. (94 deleted, 1 failed in the reported run — and the user couldn't tell.)

## Approach

Client-side **chunked** deletion against the existing endpoint — no new backend surface.

- Split selected IDs into chunks of **10**; send one chunk per request, sequentially.
- Dialog shows a **frozen total** (snapshotted at confirm) and live progress: a progress bar plus
  "Deleting `done` of `total`…".
- **Cancel** stays enabled while deleting; it stops the loop *after the current chunk finishes*.
  Already-deleted sessions stay deleted. Summary reflects what actually completed.
- Per-chunk `{succeeded, failed}` results are merged into one summary. A chunk whose request throws
  marks its IDs as failed and the loop continues (matches the endpoint's best-effort semantics).

### Why not alternatives
- **Backend SSE/WebSocket streaming** — per-1 granularity but substantial new code and surface;
  overkill for a delete.
- **Freeze count + spinner only** — fixes the count but stays opaque for minutes; does not satisfy
  "they know progress is being made."

## Components

- **`useBulkPermanentDeleteSessions`** (`web/src/api/hooks.ts`): rewritten to loop over chunks.
  Exposes `progress: { done: number; total: number } | null` and `cancel()` alongside the mutation.
  Abort tracked via a `useRef` flag checked between chunks. Replaces the up-front "remove all IDs"
  optimistic update with **incremental** removal: after each chunk resolves, remove only that
  chunk's succeeded IDs from the `QK.archived` cache so the list shrinks in step with the bar.
  Invalidate `archived` + `sessions` on settle. Clear progress on settle.
- **Inline bulk `PermanentDeleteDialog`** (`web/src/components/ArchivedManager.tsx`, ~lines 51–153):
  new optional `progress` and `onCancel` props. While pending, render the progress bar + count and
  wire Cancel → `onCancel` (abort); when not pending, Cancel → `onClose` (unchanged). Follow the
  existing progress-bar idiom from `session/CondensingProgress.tsx` (`role="progressbar"` +
  `style={{ width: ${pct}% }}`).
  Note: this is the bulk-capable inline copy, *not* the shared single-session
  `components/PermanentDeleteDialog.tsx`. Consolidating the two is tracked separately and is out of
  scope here — do not add a third variant.
- **`ArchivedManager`**: snapshot `{ ids, names }` into state when the bulk dialog opens, so the
  dialog renders from the snapshot rather than the live (shrinking) cache. `confirmBulkDelete` uses
  the snapshot's IDs.

## Data flow

confirm → snapshot frozen → `mutate(ids)` → for each chunk: POST → merge results → remove
succeeded from cache → bump `progress` → (check abort) → … → on settle: merged summary toast,
clear selection, close dialog, invalidate queries.

## Error / edge handling

- Chunk request throws → its IDs counted as failed; loop continues.
- Cancel mid-run → loop breaks at the next chunk boundary; resolves with partial
  `{succeeded, failed}` and an "stopped" indication in the summary.
- Single delete path is unchanged (no `progress` prop → no bar).
- Empty selection → no-op (existing guard).

## Testing (vitest)

- Chunking: 95 IDs → 10 requests of ≤10 IDs each.
- Progress advances across chunks; `progress` is null before/after.
- Cancel stops after the current chunk; remaining chunks are not sent.
- Merged summary counts succeeded + failed across chunks; a thrown chunk's IDs count as failed.
- Dialog renders frozen count + `progressbar` while pending; count is unaffected by cache emptying.
- Update `web/src/api/hooks.bulk.test.tsx` for the chunked contract; keep `ArchivedManager` /
  `ArchivedTab` tests green.
