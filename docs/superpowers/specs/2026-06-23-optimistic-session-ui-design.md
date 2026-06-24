# Optimistic UI for session create / archive / delete

**Date:** 2026-06-23
**Status:** Approved (brainstorming) — pending implementation plan

## Problem

Session creation, archival, and permanent deletion feel laggy and visually awkward.
Today the UI is *poll-then-reconcile*, not optimistic:

- `useSessions` refetches every 5s (`staleTime: 3_000`, `refetchInterval: 5_000`).
- Every mutation (`useCloseSession`, `usePermanentDeleteSession`, `useUnarchiveSession`,
  `useBulkUnarchiveSessions`, `useBulkPermanentDeleteSessions`) follows the same shape:
  `onSuccess → invalidateQueries`. Nothing uses `onMutate`, so the list only changes
  *after* the server round-trip resolves.
- `useCreateSession` already injects the new card via `setQueryData`, but only *after* the
  POST resolves — and that POST includes real backend spin-up of the `claude` PTY.

We want the change to appear in the UI **immediately** on user action, then reconcile with
the server. If the server rejects the action, the UI rolls back and tells the user.

## Goals

- Create, archive (close), and permanent-delete reflect instantly in the UI.
- Server failure rolls the optimistic change back and surfaces a clear message.
- Archive offers an **Undo** (it is reversible via the existing unarchive endpoint).
- No regressions from the 5s background poll racing the optimistic state.

## Non-goals

- Account mutations (add/delete/reauth/nickname) and adoption flows — out of scope this pass.
- Replacing the polling model with websocket push for the session *list* — separate concern.
- Undo for permanent delete (it is irreversible server-side and already gated by a confirm dialog).

## Core principle — optimism lives in the hooks, not components

All optimistic behavior goes into the mutation hooks in `web/src/api/hooks.ts` via React
Query's `onMutate` / `onError` / `onSettled`. The session and archived lists render in four
places — `Dashboard.tsx` (mobile inline), `DesktopShell.tsx`, `ArchivedManager.tsx`, and
`SessionCard.tsx` (confirm) — so any optimism placed in a component would drift across copies.
Components keep calling `mutate()` and stay dumb.

The standard pattern per mutation:

```
onMutate(vars):   cancelQueries(affected keys)
                  snapshot = getQueryData(keys)
                  setQueryData(keys, <apply optimistic change>)
                  return { snapshot }            // rollback context
onError(e, v, ctx): setQueryData(keys, ctx.snapshot)   // rollback
                    toast(failure message)
onSuccess(...):    success toast (+ Undo action for archive)
onSettled():       invalidateQueries(keys)       // reconcile with server truth
```

The existing `onSuccess → invalidate` calls move to `onSettled → invalidate` so reconciliation
runs on both success and error (after rollback).

## Component design

### 1. Toast system (new, minimal, reusable)

- `ToastProvider` (React context + portal) mounted once at the app root.
- `useToast()` returns `toast({ message, variant, action? })`, where
  `action?: { label: string; onClick: () => void }` renders a button (used for Undo).
- Auto-dismiss ~5s for plain toasts; longer when an `action` is present; manual dismiss too.
- **Build in-house (~100 lines), not a dependency.** The app hand-rolls its dialogs already,
  the surface is tiny, and adding a dep (e.g. `sonner`) means `package.json` + symlinked
  deploy `node_modules` churn that has bitten this repo before. Fully unit-testable with vitest.
- Styling follows existing Tailwind/shadcn tokens (reuse current button/colour tokens — do not
  introduce parallel design tokens).

### 2. Removals — archive & permanent-delete (cache-based optimism)

Applies to `useCloseSession` (archive), `usePermanentDeleteSession`, and the bulk hooks.

- `onMutate`: `cancelQueries` the affected keys (`QK.sessions` and/or `QK.archived`); snapshot;
  `setQueryData` to **remove the item(s) immediately**; return snapshot for rollback.
  - Archive removes from `QK.sessions`. We do **not** optimistically insert into the archived
    list (different shape, `ArchivedSession`); `onSettled` invalidate brings it in.
- `onError`: restore snapshot; `toast("Couldn't archive {name}")` / `"Couldn't delete {name}"`.
- `onSuccess`:
  - Archive: `toast("Archived {name}", action: { label: "Undo", onClick: unarchive(id) })`.
  - Permanent-delete: plain confirmation toast (no undo).
- `onSettled`: `invalidateQueries` affected keys.

**Bulk** (`useBulkUnarchiveSessions`, `useBulkPermanentDeleteSessions`) return
`BatchResult { succeeded, failed }`:

- `onMutate`: optimistically remove **all** targeted ids from `QK.archived`; snapshot.
- `onSuccess(result)`: any `result.failed` ids reappear via the `onSettled` invalidate (the
  server still has them); toast summarizes, e.g. `"Deleted 3 · 1 failed"` or `"Deleted 4"`.
  No undo on bulk.
- `onError` (whole request failed): restore the full snapshot; toast.
- `onSettled`: invalidate.

Existing confirm dialogs (`SessionCard.tsx`, `ArchivedManager.tsx`) remain the pre-action guard.

### 3. Create — placeholder card + dialog stays open (client-store optimism)

PTY spin-up can exceed the 5s poll interval, so a poll *will* fire mid-creation. A placeholder
that lived only in the query cache would be wiped by that refetch. Therefore create placeholders
live **outside** the query cache:

- A small client-side `pendingSessions` store (a lightweight provider holding React state, or a
  module-level store with `useSyncExternalStore`) holds optimistic placeholder `Session`s, each
  with a temp id (`temp-<n>`) and a client-only `pending: true` flag.
- Add a client-only optional field to the `Session` type: `pending?: boolean` (never sent by the
  server). Consumers of the session list merge `pendingSessions` into the rendered list so a
  background poll cannot remove them.
- On `mutate`: push a placeholder → its card renders instantly in a disabled **"starting…"**
  state; the create dialog (`DispatchBar` / new-session dialog) **stays open with its spinner**.
- `onSuccess`: remove the placeholder from the store; reconcile the real session into the cache
  via `setQueryData` (swap to avoid a flicker) and let the next poll confirm; close the dialog.
- `onError`: remove the placeholder; **keep the dialog open with the inline error** (the existing
  `DispatchBar` error pattern). No list-level rollback drama. A toast is optional here since the
  dialog already shows the failure.

### 4. Polling-race handling

- Removals: `cancelQueries` in `onMutate` cancels any in-flight refetch; the mutation almost
  always settles well within the 5s window, after which `onSettled` invalidate reconciles. We
  accept the negligible flicker risk and do **not** add a tombstone set unless flicker is
  observed in practice (YAGNI).
- Create: solved structurally by the out-of-cache `pendingSessions` store (above) — polls cannot
  touch it.

## Data flow summary

| Action            | Optimistic effect (immediate)        | On success                          | On error                          |
|-------------------|--------------------------------------|-------------------------------------|-----------------------------------|
| Create            | "starting…" placeholder card         | swap to real session, close dialog  | remove placeholder, dialog error  |
| Archive (close)   | card removed from sessions           | toast + **Undo**                    | card restored, failure toast      |
| Permanent delete  | row removed from archived            | confirmation toast                  | row restored, failure toast       |
| Bulk unarchive    | rows removed from archived           | failed rows reappear; summary toast | all restored, failure toast       |
| Bulk delete       | rows removed from archived           | failed rows reappear; summary toast | all restored, failure toast       |

## Testing

- **Toast system**: unit tests — render, auto-dismiss, manual dismiss, action button fires callback.
- **Hooks** (vitest, mirroring `hooks.createSession.test.tsx`): for each mutation assert the cache
  changes synchronously on `mutate` (optimistic), rolls back on a mocked rejection, fires the
  expected toast, and invalidates on settle. Archive test asserts the Undo action calls unarchive.
- **Create**: placeholder appears immediately, survives a simulated background refetch, reconciles
  on success, and is removed with the dialog held open + error shown on failure.
- **Bulk**: partial-failure path re-shows failed ids and reports the right summary.
- Keep both rendering paths green (Dashboard mobile + DesktopShell/ArchivedManager) — logic in the
  hooks means component tests should need no optimism-specific changes beyond the toast provider.

## Affected files (anticipated)

- `web/src/api/hooks.ts` — add `onMutate`/`onError`, move `onSuccess`→`onSettled` invalidate.
- `web/src/api/types.ts` — add client-only `Session.pending?: boolean`.
- New: toast provider/hook + `pendingSessions` store under `web/src/components/` or `web/src/lib/`.
- App root (`App.tsx`) — mount `ToastProvider`.
- `DispatchBar.tsx` — keep dialog open until create settles; show inline error on failure.
- Tests as above.

## Related prior work

- `docs/superpowers/specs/2026-06-21-eager-session-create-design.md` — the earlier eager-create
  pass that added the post-success `setQueryData`. This design supersedes its create behavior with
  a true pre-response placeholder.
- `docs/superpowers/specs/2026-06-22-error-delivery-to-ui-design.md` — existing error-surfacing
  approach; the new toast system should complement, not duplicate, it.
