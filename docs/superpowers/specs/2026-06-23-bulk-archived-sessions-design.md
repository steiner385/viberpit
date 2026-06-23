# Bulk Operations for the Archived Sessions Screen

**Date:** 2026-06-23
**Status:** Approved — ready for implementation plan

## Problem

The archived sessions screen (`web/src/components/ArchivedManager.tsx`) only exposes
per-session actions: each archived session has its own **Review**, **Unarchive**, and
**permanent-delete** buttons. Clearing out a backlog of archived sessions — or restoring
several at once — means clicking through them one at a time. Users want to select multiple
archived sessions and unarchive or permanently delete them in a single action.

## Goals

- Select multiple archived sessions and **unarchive** them in one action.
- Select multiple archived sessions and **permanently delete** them in one action, behind a
  confirmation dialog (the action is irreversible).
- Tolerate partial failure: one bad session (e.g. an orphaned archive folder, a row already
  removed by a sibling worktree) must not block the rest of the batch.
- Preserve the existing per-session audit granularity.

## Non-goals

- Bulk **Review** — review opens a single read-only transcript and is inherently single-session.
- Bulk operations on *active* (non-archived) sessions — out of scope for this change.
- Cross-screen selection or persistence of selection across refetches.

## Current state (as explored)

- **Frontend:** `ArchivedManager.tsx` — `ArchivedManager` owns the panel data and the
  permanent-delete confirmation; `ArchivedList` renders a `<ul>` of sessions with per-row
  Review / Unarchive / delete buttons; `PermanentDeleteDialog` is a focus-trapped modal with a
  red "Delete permanently" action. No checkbox or multi-select UI exists anywhere in `web/src/`.
- **Hooks:** `web/src/api/hooks.ts` — `useArchivedSessions()` (query key `QK.archived`),
  `useUnarchiveSession()`, `usePermanentDeleteSession()`. The two mutations invalidate both
  `QK.archived` and `QK.sessions` on success.
- **Backend:** `server/api/sessions.py` — `GET /api/sessions/archived`,
  `POST /api/sessions/{id}/unarchive`, `DELETE /api/sessions/{id}/permanent`. All single-ID.
  A load-bearing comment at `sessions.py:359` requires static routes to be declared **before**
  the dynamic `/{session_id}` route.
- **Orchestrator:** the API calls `orch.unarchive(id)` / `orch.permanent_delete(id)` through
  `server/sessions/facade.py`, which dispatches to either the in-process orchestrator or the
  remote driver. The remote path is known to be under-tested (project memory:
  `remote-mode-path-asymmetry`), so we avoid adding new methods to that surface.
- **Data model:** archived state is `Session.status == "archived"` in `server/db/models.py` —
  no schema change is needed.

## Design

### Backend — two new batch endpoints

Add to `server/api/sessions.py`, **declared before** the dynamic `/{session_id}` route so
"batch" is never matched as a session ID:

- `POST /api/sessions/batch/unarchive`
- `POST /api/sessions/batch/permanent-delete`

Both accept the same request body, validated by a new Pydantic model:

```python
class BatchSessionIdsRequest(BaseModel):
    ids: list[str]  # non-empty; deduplicated; capped at a sane max (e.g. 200)
```

`POST` (not `DELETE`) is used for permanent-delete because DELETE-with-body is unreliable
across clients and proxies.

**Implementation — reuse single-ID orchestrator methods in a loop.** Each endpoint iterates
the IDs and calls the *existing* `orch.unarchive(id)` / `orch.permanent_delete(id)`, wrapping
each call in try/except. This reuses already-wired logic that works in both in-process and
remote modes, and adds no new orchestrator / RPC / remote-driver surface.

**Best-effort semantics with a per-ID report.** Both endpoints return **200** with:

```json
{
  "succeeded": ["id1", "id2"],
  "failed": [{ "id": "id3", "error": "not found" }]
}
```

- A `None`/falsy return from the orchestrator (session not found / not archived) → `failed`
  entry with `error: "not found"`.
- A raised exception → `failed` entry with a sanitized `error` message; the loop continues.
- Empty `ids` after validation → `422` (Pydantic). Oversized list → `422`.

**Audit.** Each *successful* unarchive/delete writes its existing audit record
(`session.unarchive` / `session.permanent_delete`) with that `session_id`, preserving
per-session granularity. Failures are not audited as successes.

### Frontend — selection state + bulk action bar

In `ArchivedManager.tsx`:

- **Selection state** lives in `ArchivedManager` as `selected: Set<string>`. A row is selected
  via a per-row checkbox; a header "select all" checkbox toggles every currently-listed ID
  (with an indeterminate state when some-but-not-all are selected). Selection is cleared after
  a successful bulk op and when the list refetches out IDs that no longer exist.
- **Bulk action bar** renders only when `selected.size >= 1`. It shows the selected count and
  three controls: **Unarchive (N)**, **Delete (N)** (red/destructive styling), and **Clear**.
- **Bulk unarchive** applies immediately via the new hook.
- **Bulk delete** opens a confirmation dialog. The existing `PermanentDeleteDialog` is
  generalized to accept either a single session or a count + list of names, so the destructive
  confirmation pattern (focus trap, Escape-to-close, red confirm button) is reused rather than
  duplicated.
- **Result feedback** after a batch completes: a brief summary such as
  "Unarchived 8 · 2 failed". Failed-because-missing IDs disappear naturally on the next refetch.

### Frontend — two new hooks

In `web/src/api/hooks.ts`, mirroring the single-ID mutations:

- `useBulkUnarchiveSessions()` → `POST /api/sessions/batch/unarchive`, body `{ ids }`.
- `useBulkPermanentDeleteSessions()` → `POST /api/sessions/batch/permanent-delete`, body `{ ids }`.

Both invalidate `QK.archived` **and** `QK.sessions` on success (a restored session reappears in
the active list; a deleted one may also have been live). Both return the
`{ succeeded, failed }` payload so the UI can render the summary.

A matching `BatchResult` type is added to `web/src/api/types.ts`.

## Error handling

- **Partial failure** is the normal, expected path — surfaced via the `{ succeeded, failed }`
  report, never as an HTTP error.
- **Auth:** both endpoints require a verified identity via `current_identity`, returning 401
  for unknown identities, exactly like the existing routes.
- **Validation:** empty or oversized `ids` → 422 from Pydantic; the UI disables the action-bar
  buttons when nothing is selected, so an empty request shouldn't occur in normal use.

## Testing

**Backend (`server/tests/`):**
- `batch/unarchive` and `batch/permanent-delete`: all-success path.
- Partial failure: a mix of valid IDs and a non-existent / non-archived ID returns 200 with the
  bad ID in `failed` and the good ones in `succeeded`, and the good ones are actually
  unarchived / deleted.
- Empty `ids` → 422; oversized `ids` → 422.
- 401 when the identity is not allowlisted.
- One audit record per *successful* op.

**Frontend (`web/`, vitest):**
- Selecting rows updates the count; the action bar appears only with ≥1 selected.
- Select-all toggles all rows; indeterminate state when partial.
- Bulk delete opens the confirm dialog and only fires on confirm; Cancel/Escape aborts.
- Bulk unarchive fires immediately and calls the hook with the selected IDs.
- Selection clears after a successful op.

## Files touched

- `server/api/sessions.py` — `BatchSessionIdsRequest` model + two endpoints (before `/{id}`).
- `server/tests/` — new endpoint tests.
- `web/src/api/hooks.ts` — two new mutation hooks.
- `web/src/api/types.ts` — `BatchResult` type.
- `web/src/components/ArchivedManager.tsx` — selection state, checkboxes, action bar,
  generalized confirm dialog.
- `web/` test files for the above.
