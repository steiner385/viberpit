# Global Mutation-Error Toast Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface every failed mutation app-wide via a `sonner` toast, driven by a single global TanStack Query `MutationCache.onError`.

**Architecture:** A new `web/src/api/queryClient.ts` becomes the single source of truth for the app's `QueryClient`, wiring a `MutationCache` whose `onError` derives a human message from `ApiRequestError` and shows `toast.error(...)`. `main.tsx` consumes that client and mounts sonner's `<Toaster />`. No call-site changes; mutations opt out via `meta: { suppressErrorToast: true }`.

**Tech Stack:** React + TypeScript, TanStack Query v5, sonner, vitest + Testing Library.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-06-23-global-mutation-error-toast-design.md`.
- `sonner` is the toast lib (already added to `web/package.json` as `^2.0.7`; the real `web/node_modules` and updated `web/package-lock.json` are in place). CI runs `npm ci`, so the lockfile MUST be committed.
- Fallback message constant (exact): `Something went wrong. Please try again.`
- Message precedence for `ApiRequestError`: `body.detail.message` (string) → `body.message` (string) → fallback. Non-`ApiRequestError` → fallback.
- Opt-out: skip the toast when `mutation.options.meta?.suppressErrorToast` is truthy. Do NOT annotate any existing mutation with it (YAGNI — extension point only).
- Toaster props (exact): `richColors position="top-center" theme="system"`.
- No changes to `SessionActions` or to `useCloseSession`/`usePermanentDeleteSession`.
- Web type gate is `tsc -b` (NOT `--noEmit`). Lint runs with `--max-warnings 0`. TDD required for the logic task.
- Test runner: local `./node_modules/.bin/vitest` / `./node_modules/.bin/eslint`; type gate `npx tsc -b --force`. Do NOT run `npm install` again (already done).

---

### Task 1: `queryClient.ts` — configured client + error helper + handler (TDD)

**Files:**
- Create: `web/src/api/queryClient.ts`
- Test: `web/src/api/queryClient.test.ts`
- Commit also includes: `web/package.json`, `web/package-lock.json` (the already-staged `sonner` dependency addition).

**Interfaces:**
- Consumes: `ApiRequestError` from `@/api/client`; `toast` from `sonner`; `QueryClient`, `MutationCache`, type `Mutation` from `@tanstack/react-query`.
- Produces (from `@/api/queryClient`):
  - `extractMutationErrorMessage(error: unknown): string`
  - `handleMutationError(error: unknown, mutation: Mutation<unknown, unknown, unknown>): void`
  - `queryClient: QueryClient`
  - `MUTATION_ERROR_FALLBACK: string` (the fallback constant, exported so the test references one source of truth)

- [ ] **Step 1: Write the failing test**

Create `web/src/api/queryClient.test.ts`:

```ts
import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { Mutation } from '@tanstack/react-query'
import { ApiRequestError } from '@/api/client'
import {
  extractMutationErrorMessage,
  handleMutationError,
  MUTATION_ERROR_FALLBACK,
} from './queryClient'

vi.mock('sonner', () => ({ toast: { error: vi.fn() } }))
import { toast } from 'sonner'

// Minimal stand-in for a TanStack Mutation — handleMutationError only reads options.meta.
function fakeMutation(meta?: Record<string, unknown>) {
  return { options: { meta } } as unknown as Mutation<unknown, unknown, unknown>
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('extractMutationErrorMessage', () => {
  it('uses body.detail.message when present', () => {
    const err = new ApiRequestError(409, { detail: { code: 'resume_failed', message: 'Session could not be resumed.' } })
    expect(extractMutationErrorMessage(err)).toBe('Session could not be resumed.')
  })

  it('falls back to body.message when there is no detail.message', () => {
    const err = new ApiRequestError(400, { message: 'Bad request' })
    expect(extractMutationErrorMessage(err)).toBe('Bad request')
  })

  it('returns the fallback when an ApiRequestError carries no usable message', () => {
    const err = new ApiRequestError(500, {})
    expect(extractMutationErrorMessage(err)).toBe(MUTATION_ERROR_FALLBACK)
  })

  it('returns the fallback for a non-API error', () => {
    expect(extractMutationErrorMessage(new Error('network boom'))).toBe(MUTATION_ERROR_FALLBACK)
  })
})

describe('handleMutationError', () => {
  it('shows an error toast with the extracted message', () => {
    const err = new ApiRequestError(404, { detail: { code: 'not_found', message: 'Session not found' } })
    handleMutationError(err, fakeMutation())
    expect(toast.error).toHaveBeenCalledTimes(1)
    expect(toast.error).toHaveBeenCalledWith('Session not found')
  })

  it('does not toast when the mutation opts out via meta.suppressErrorToast', () => {
    const err = new ApiRequestError(404, { detail: { code: 'not_found', message: 'Session not found' } })
    handleMutationError(err, fakeMutation({ suppressErrorToast: true }))
    expect(toast.error).not.toHaveBeenCalled()
  })
})
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd web && ./node_modules/.bin/vitest run src/api/queryClient.test.ts
```
Expected: FAIL — cannot resolve `./queryClient`.

- [ ] **Step 3: Write the module**

Create `web/src/api/queryClient.ts`:

```ts
/**
 * queryClient — the app's single TanStack Query client, wired with a global
 * MutationCache.onError so any failed mutation surfaces a sonner error toast.
 *
 * A mutation can opt out of the global toast (e.g. when it renders its own inline
 * error) by setting `meta: { suppressErrorToast: true }`. No mutation does so today;
 * it is a documented extension point.
 */
import { QueryClient, MutationCache, type Mutation } from '@tanstack/react-query'
import { toast } from 'sonner'
import { ApiRequestError } from '@/api/client'

export const MUTATION_ERROR_FALLBACK = 'Something went wrong. Please try again.'

/** Derive a human-readable message from a mutation error. */
export function extractMutationErrorMessage(error: unknown): string {
  if (error instanceof ApiRequestError) {
    const detail = error.body?.detail
    if (detail && typeof detail === 'object' && 'message' in detail) {
      const msg = (detail as Record<string, unknown>).message
      if (typeof msg === 'string' && msg.trim() !== '') return msg
    }
    const top = error.body?.message
    if (typeof top === 'string' && top.trim() !== '') return top
  }
  return MUTATION_ERROR_FALLBACK
}

/** Global MutationCache.onError handler: toast unless the mutation opts out. */
export function handleMutationError(
  error: unknown,
  mutation: Mutation<unknown, unknown, unknown>,
): void {
  if (mutation.options.meta?.suppressErrorToast) return
  toast.error(extractMutationErrorMessage(error))
}

export const queryClient = new QueryClient({
  mutationCache: new MutationCache({
    // v5 onError signature: (error, variables, onMutateResult, mutation, context)
    onError: (error, _variables, _onMutateResult, mutation) => handleMutationError(error, mutation),
  }),
})
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
cd web && ./node_modules/.bin/vitest run src/api/queryClient.test.ts
```
Expected: PASS (6 tests).

- [ ] **Step 5: Type-check + lint**

```bash
cd web && npx tsc -b --force
cd web && ./node_modules/.bin/eslint src/api/queryClient.ts src/api/queryClient.test.ts --max-warnings 0
```
Expected: clean. (If `mutation.options.meta?.suppressErrorToast` trips a type error because `MutationMeta` is unindexed, read it as `(mutation.options.meta as Record<string, unknown> | undefined)?.suppressErrorToast` — but try the direct form first; v5's `MutationMeta` is `Record<string, unknown>`.)

- [ ] **Step 6: Commit**

```bash
git add web/src/api/queryClient.ts web/src/api/queryClient.test.ts web/package.json web/package-lock.json
git commit -m "feat(web): add global mutation-error toast handler + sonner dep"
```

---

### Task 2: Wire the shared client + `<Toaster />` into `main.tsx`

**Files:**
- Modify: `web/src/main.tsx`

**Interfaces:**
- Consumes: `queryClient` from `@/api/queryClient` (Task 1); `Toaster` from `sonner`.

- [ ] **Step 1: Replace the bare client and mount the Toaster**

`web/src/main.tsx` currently is:

```tsx
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { BrowserRouter } from 'react-router-dom'
import './index.css'
import App from './App.tsx'

const queryClient = new QueryClient()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
)
```

Replace its whole contents with:

```tsx
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClientProvider } from '@tanstack/react-query'
import { BrowserRouter } from 'react-router-dom'
import { Toaster } from 'sonner'
import './index.css'
import App from './App.tsx'
import { queryClient } from '@/api/queryClient'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <App />
        <Toaster richColors position="top-center" theme="system" />
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
)
```

(Note: `QueryClient` is no longer imported directly here — only `QueryClientProvider`.)

- [ ] **Step 2: Type-check + lint**

```bash
cd web && npx tsc -b --force
cd web && ./node_modules/.bin/eslint src/main.tsx --max-warnings 0
```
Expected: clean (no unused `QueryClient` import warning).

- [ ] **Step 3: Full web gate (CI runs the full suite)**

```bash
cd web && npx tsc -b --force
cd web && ./node_modules/.bin/eslint src --max-warnings 0
cd web && ./node_modules/.bin/vitest run
```
Expected: all green. `main.tsx` is an entry module not imported by tests, so existing tests (which build their own `QueryClient`) are unaffected.

- [ ] **Step 4: Commit**

```bash
git add web/src/main.tsx
git commit -m "feat(web): mount sonner Toaster and use the global query client"
```

---

## Self-Review

**1. Spec coverage**
- sonner added → Task 1 commit (package.json/lock). ✓
- Global `MutationCache.onError` → Task 1 `queryClient`. ✓
- `meta.suppressErrorToast` opt-out, not pre-applied → Task 1 `handleMutationError`; no other file annotated. ✓
- Message precedence (`detail.message` → `message` → fallback) → Task 1 `extractMutationErrorMessage` + tests. ✓
- Toaster mounted with exact props → Task 2. ✓
- No change to SessionActions/hooks → neither task touches them. ✓
- Existing tests unaffected → Task 2 Step 3 verifies full suite. ✓

**2. Placeholder scan:** No TBD/TODO; full code in every code step. The one conditional (Task 1 Step 5 type-coercion fallback) is an explicit, bounded instruction.

**3. Type consistency:** `extractMutationErrorMessage` / `handleMutationError` / `MUTATION_ERROR_FALLBACK` / `queryClient` names match between `queryClient.ts`, its test, and `main.tsx`. `handleMutationError(error, mutation)` signature matches its call from the `MutationCache.onError` adapter `(error, _v, _c, mutation) => handleMutationError(error, mutation)`.
