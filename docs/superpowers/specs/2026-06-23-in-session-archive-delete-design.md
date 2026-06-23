# In-Session Archive & Delete — Design

**Date:** 2026-06-23
**Feature:** Surface "Archive" and "Delete" actions inside the live session view.

## Problem

A user viewing a live session can only archive or delete it by leaving the session
and returning to the session list (`SessionCard`) or the Archived tab. There is no way
to archive or permanently delete the session you are currently attached to. We want both
actions available from within the session view, on both the mobile full-screen layout and
the desktop master-detail layout.

## Decisions

1. **UI placement:** Archive is a primary action in the session header; Delete lives behind
   an overflow "⋯" menu (reduces accidental destructive taps; leaves room for future
   session actions like rename/export).
2. **Delete semantics (live session):** archive-then-delete. Stop and archive the running
   session first, then permanently delete it. This cleanly tears down the live driver instead
   of leaving a zombie in-memory record, and composes the two existing endpoints rather than
   adding a new "delete a live session" backend path.
3. **Archive confirmation:** none — archive immediately on click (archiving is reversible via
   the Archived tab, and matches the existing `SessionCard` archive button). Delete keeps a
   confirmation via the existing dialog.

## Architecture & Reuse

No new backend endpoints or orchestrator methods. Both actions route through existing hooks
and the existing confirmation dialog, honoring the project's single-source-of-truth /
no-parallel-implementations rule.

- **Archive** → `useCloseSession()` → `DELETE /api/sessions/{id}` (stops driver, moves working
  dir to archive, marks status `archived`).
- **Delete** → `useCloseSession()` then, on success, `usePermanentDeleteSession()` →
  `DELETE /api/sessions/{id}/permanent` (removes archive folder + DB row). Sequenced
  client-side.
- **Confirm dialog** → reuse `PermanentDeleteDialog` (already used by `Dashboard` and
  `ArchivedManager`).

### Why archive-then-delete (background)

`orchestrator.permanent_delete` always builds its candidate path as `archive_dir() / session_id`
with `rmtree(..., ignore_errors=True)`. For a live session whose working dir is not yet in the
archive, that rmtree is a no-op: it removes the DB row but does **not** stop the running driver or
clear the in-memory record. Archiving first routes through `archive_session`, which stops the
driver, clears in-memory state, and moves the dir into the archive — so the subsequent
permanent-delete operates on a genuinely archived session and leaves nothing running.

## Components

A new shared component **`SessionActions`** under `web/src/components/session/`:

- Renders the Archive button + overflow "⋯" menu (with Delete).
- Owns the Delete confirmation state and renders the existing `PermanentDeleteDialog`.
- Takes the session `id`, display `name`/`title`, and an `onDone` callback (invoked after a
  successful archive or delete to detach + navigate away).

Rendered in **both** headers so behavior is identical across layouts:

- `web/src/components/session/SessionView.tsx` — mobile full-screen header, beside the title.
- `web/src/components/DesktopShell.tsx` (`MainPaneSession`) — desktop master-detail header.

## Data & Control Flow

1. **Archive:** click → `closeSession.mutate(id)` → on success: `onDone()` (detach WebSocket +
   navigate back to `/sessions`; desktop clears the selected session).
2. **Delete:** "⋯" → Delete → `PermanentDeleteDialog` confirm → `closeSession.mutate(id)` →
   on success `permanentDeleteSession.mutate(id)` → on success: `onDone()`.
3. **Teardown/navigation:** reuse the existing `handleBack`/`onBack` path, which already calls
   `detach(sessionId)` then navigates. `onDone` wraps that so we do not duplicate teardown.
4. Cache invalidation (`QK.sessions`, `QK.archived`) already happens inside the hooks.

## Error Handling

- On any mutation error, **stay in the session** (do not navigate) and surface the error via the
  existing toast/error pattern, so a failed delete does not dump the user out.
- Archive-then-delete: if the archive step fails, abort before deleting and show the error. If
  archive succeeds but delete fails, the session is now archived (recoverable from the Archived
  tab) — report that state rather than silently swallowing it; do not navigate.

## Testing

- **Web (vitest):** `SessionActions` —
  - Archive calls `useCloseSession` then `onDone`.
  - Delete runs close → permanent-delete in order and calls `onDone` only after both resolve.
  - Error paths do not call `onDone` (user stays in session).
  - Delete shows the confirm dialog; Archive does not.
- **Gates:** `tsc -b` (the real type gate — `tsc --noEmit` checks nothing here) and `eslint`
  (CI runs with `--max-warnings 0`).
- **Backend:** no changes; confirm existing endpoint tests still cover archive + permanent-delete.

## Out of Scope

- Changing `permanent_delete` to stop a live driver itself (would fix Dashboard's direct-delete
  path too, but has a larger blast radius on shared backend behavior). The archive-then-delete
  composition avoids the issue for this feature without that change.
- Bulk in-session actions, rename, or other session-header controls.
