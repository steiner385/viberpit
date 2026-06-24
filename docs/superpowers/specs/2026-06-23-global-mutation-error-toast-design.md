# Global Mutation-Error Toast — Design

**Date:** 2026-06-23
**Feature:** Surface failed mutations app-wide via a toast notification.

## Problem

The web app has no transient-notification infrastructure. Failed mutations are
silently swallowed unless a component wired its own `onError` (almost none do).
`web/src/main.tsx` constructs a bare `new QueryClient()` with no global error
handling. The in-session archive/delete (`SessionActions`) is one instance of this
gap: a failed archive or permanent-delete produces no user-facing feedback. The fix
is a single global handler so every failed mutation reports, rather than patching one
call site.

## Decisions

1. **Library:** use `sonner` (small, vetted) for the toast UI rather than hand-rolling.
2. **Trigger:** a global TanStack Query `MutationCache.onError` on the app's single
   `QueryClient`, so all mutations are covered by default.
3. **Opt-out:** a mutation may set `meta: { suppressErrorToast: true }` to handle its
   own errors inline. No mutation opts out today; it is a documented extension point
   (not pre-applied — YAGNI).
4. **Message:** derive a human message from `ApiRequestError`; fall back to a generic
   string when none is available.
5. **Position/theme:** `position="top-center"`, `richColors` (red error styling),
   `theme="system"` (respects dark mode). Sonner provides the aria-live semantics.

## Architecture & Files

- **`web/package.json` + `web/package-lock.json`** — add `sonner` (^2.0.7).
- **`web/src/api/queryClient.ts` (new)** — single source of truth for the app
  `QueryClient`. Exports:
  - `extractMutationErrorMessage(error: unknown): string` — pure helper. For an
    `ApiRequestError`, returns `body.detail.message` if present, else `body.message`
    if present, else the fallback. For any other error, returns the fallback.
    Fallback constant: `"Something went wrong. Please try again."`
  - `handleMutationError(error: unknown, mutation: Mutation): void` — the
    `MutationCache.onError` callback. Returns early when
    `mutation.options.meta?.suppressErrorToast` is truthy; otherwise calls
    `toast.error(extractMutationErrorMessage(error))`. Exported for unit testing
    without rendering.
  - `queryClient` — `new QueryClient({ mutationCache: new MutationCache({ onError: handleMutationError }) })`.
- **`web/src/main.tsx`** — import `queryClient` from `@/api/queryClient` (replace the
  bare `new QueryClient()`); render `<Toaster richColors position="top-center" theme="system" />`
  inside the `QueryClientProvider` tree.

`ApiRequestError` (web/src/api/client.ts) has `status: number` and
`body: Record<string, unknown>`. The backend error envelope is
`{ detail: { code, message } }` (FastAPI `HTTPException(detail={...})`), so
`body.detail.message` is the primary source.

## Why no change to SessionActions / hooks

`SessionActions` and the `useCloseSession` / `usePermanentDeleteSession` hooks keep
their existing `onSuccess`-only wiring. On failure, `onDone` already does not fire (the
user stays in the session) and the delete dialog stays open to retry; the global
handler now adds the toast. Nothing in those files needs to change.

## Error Handling Semantics

- Archive fails → toast; `onDone` not called (stay in session).
- Permanent-delete fails after a successful archive → toast; the confirm dialog stays
  open. (The session is already archived and recoverable from the Archived tab.)

## Testing

- **`extractMutationErrorMessage`** (pure): `ApiRequestError` with `detail.message`;
  with bare `body.message`; with neither → fallback; a non-API `Error` → fallback.
- **`handleMutationError`** with `sonner` mocked (`vi.mock('sonner')`): calls
  `toast.error` with the extracted message; does NOT call `toast.error` when
  `mutation.options.meta.suppressErrorToast` is set.
- Existing tests construct their own `QueryClient`, so they are unaffected by the
  global handler (low blast radius). No existing test should need changes.

## Out of Scope

- Success/info toasts (only error toasts now).
- Per-mutation custom messages or retry actions in the toast.
- Migrating existing inline error displays (reauth/add-account status lines, transcript
  ErrorCard) to toasts — those stay as-is.

## Environment Note

This fresh worktree's `web/node_modules` was a symlink to a sibling worktree; adding
`sonner` required replacing it with a real `npm install`. CI runs `npm ci` against the
updated `package-lock.json`, so the lockfile must be committed.
