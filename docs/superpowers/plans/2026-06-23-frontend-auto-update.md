# Frontend Auto-Update on New Version — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (this
> project mandates subagent-driven execution) to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** The browser detects when it's running an outdated bundle after a deploy and updates to the
current one — a banner immediately, plus a silent reload when it's safe (focused session idle + no
buffered prompts).

**Architecture:** Inject the build's git SHA at compile time (`__BUILD_SHA__`). On every WebSocket
reconnect (a deploy always restarts the edge → reconnect), fetch `/healthz`, compare its running
`sha` to `__BUILD_SHA__`; on mismatch, set a sticky `updateAvailable`. A global, fixed-position
component (mounted in `App`, above both the desktop and mobile layouts) shows a "New version
available — Reload" banner and auto-reloads `location.reload()` when the focused session isn't
mid-turn and no prompts are client-buffered. Unsent drafts survive via existing localStorage.

**Tech Stack:** React/TypeScript, Vite (`define`), Vitest, TanStack Query.

## Global Constraints

- Run web tests/types/lint with LOCAL bins from `web/` (node_modules is a symlink): `cd web &&
  ./node_modules/.bin/vitest run <files>`, `./node_modules/.bin/tsc -b --force` (the REAL type gate —
  NOT `tsc --noEmit`), `./node_modules/.bin/eslint <files> --max-warnings 0`.
- **Adding a hook/import to `App.tsx` or `Dashboard.tsx` can break sibling tests via their module-mock
  factories — `tsc`/`eslint` miss it, only a broad `vitest run` catches it.** Any task touching
  `App.tsx` MUST run the broad suite (`./node_modules/.bin/vitest run`) and confirm no NEW failures.
- Wire `type` / event-name reuse from the drain feature: `getSessionSocket()`, the
  `updatePendingChange` event, the `get updatePendingState(): {active:boolean;count:number}` getter,
  and `useUpdatePending()` already exist — reuse them, do not reimplement (constitution VI).
- Detection is **reconnect-triggered only** (no polling). Gated by `import.meta.env.PROD` so dev/test
  never auto-reload or fetch `/healthz`.
- `/healthz` is **same-origin** (`fetch('/healthz')`, no `/api` prefix) and returns `{"sha":"<7char>"}`.
- TDD: failing test first each task; commit after each. Branch `feat/frontend-auto-update` (off
  latest `master`) already exists with the design spec committed.

## Exact signatures this plan relies on (verified)

- `web/src/ws/client.ts`: `export type ConnectionState = 'connecting'|'connected'|'disconnected'|'reconnecting'`;
  `getSessionSocket(): SessionSocket`; `socket.on/off('connectionStateChange', (s: ConnectionState)=>void)`;
  `get connectionState`; `get activeSessionId(): string | null`; `get updatePendingState(): {active:boolean;count:number}`.
- `web/src/api/types.ts`: `SessionStatus = 'idle'|'in_progress'|'waiting_input'|'waiting_question'|'ready_review'|'done'|'complete'|'needs_attention'|'limited'|'ended'`;
  `Session` has `id: string` and `status: SessionStatus`.
- `web/src/api/hooks.ts`: `useSessions()` → `useQuery<Session[]>` (refetch every 5s, also patched by WS status frames).
- `web/src/hooks/useUpdatePending.ts`: `useUpdatePending(): {active:boolean;count:number}` (reactive to `updatePendingChange`).
- `web/src/App.tsx`: `function App()` returns `isWide ? <DesktopShell/> : <Routes>…</Routes>` (line ~86-107).
- `web/vite.config.ts`: `defineConfig({ plugins:[react(),tailwindcss()], resolve:{…}, server:{proxy:{…}} })` — no `define` yet.
- No `web/src/vite-env.d.ts` exists yet.

---

### Task 1: Build-SHA injection + version-compare helper

Inject `__BUILD_SHA__` at build time and provide a pure, tested comparison helper.

**Files:**
- Modify: `web/vite.config.ts`
- Create: `web/src/vite-env.d.ts`
- Create: `web/src/lib/version.ts`
- Test: `web/src/lib/version.test.ts`

**Interfaces:**
- Produces: global `__BUILD_SHA__: string` (via vite define); `BUILD_SHA: string` (typeof-guarded constant in `lib/version.ts`); `isOutdated(buildSha: string, serverSha: string | null | undefined): boolean`; `isSafeToReload(focusedStatus: string | undefined, bufferedCount: number): boolean`.

- [ ] **Step 1: Write the failing test**

Create `web/src/lib/version.test.ts`:

```typescript
import { describe, it, expect } from 'vitest'
import { isOutdated, isSafeToReload } from './version'

describe('isOutdated', () => {
  it('false when shas match', () => {
    expect(isOutdated('abc1234', 'abc1234')).toBe(false)
  })
  it('true when shas differ', () => {
    expect(isOutdated('abc1234', 'def5678')).toBe(true)
  })
  it('false when server sha is missing (cannot tell)', () => {
    expect(isOutdated('abc1234', null)).toBe(false)
    expect(isOutdated('abc1234', undefined)).toBe(false)
    expect(isOutdated('abc1234', '')).toBe(false)
  })
  it('false when build sha is the dev placeholder', () => {
    expect(isOutdated('dev', 'abc1234')).toBe(false)
  })
})

describe('isSafeToReload', () => {
  it('unsafe when prompts are buffered', () => {
    expect(isSafeToReload('idle', 1)).toBe(false)
  })
  it('unsafe when the focused session is mid-turn', () => {
    expect(isSafeToReload('in_progress', 0)).toBe(false)
    expect(isSafeToReload('waiting_input', 0)).toBe(false)
    expect(isSafeToReload('waiting_question', 0)).toBe(false)
  })
  it('safe when idle / no focused session / terminal statuses, nothing buffered', () => {
    expect(isSafeToReload('idle', 0)).toBe(true)
    expect(isSafeToReload(undefined, 0)).toBe(true)
    expect(isSafeToReload('done', 0)).toBe(true)
    expect(isSafeToReload('ready_review', 0)).toBe(true)
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && ./node_modules/.bin/vitest run src/lib/version.test.ts`
Expected: FAIL — `./version` not found.

- [ ] **Step 3: Create the helper**

Create `web/src/lib/version.ts`:

```typescript
// The bundle's build sha, injected by vite.config.ts `define`. Read through a typeof guard so a
// test/build environment that didn't apply the define resolves to 'dev' instead of throwing a
// ReferenceError. Single source of truth for reading the global (constitution VI).
export const BUILD_SHA: string = typeof __BUILD_SHA__ !== 'undefined' ? __BUILD_SHA__ : 'dev'

// Statuses where the focused session is actively mid-turn — reloading would interrupt a
// stream the user is watching. (Other statuses are terminal/idle and safe to reload through.)
const UNSAFE_STATUSES = new Set(['in_progress', 'waiting_input', 'waiting_question'])

/** True when the loaded bundle's sha differs from the server's running sha. Conservative:
 *  returns false when the server sha is unknown, or the build is the dev placeholder. */
export function isOutdated(buildSha: string, serverSha: string | null | undefined): boolean {
  if (!serverSha) return false
  if (buildSha === 'dev') return false
  return buildSha !== serverSha
}

/** True when an auto-reload would not lose work or interrupt a watched stream:
 *  no client-buffered prompts, and the focused session (if any) is not mid-turn. */
export function isSafeToReload(focusedStatus: string | undefined, bufferedCount: number): boolean {
  if (bufferedCount > 0) return false
  if (focusedStatus && UNSAFE_STATUSES.has(focusedStatus)) return false
  return true
}
```

- [ ] **Step 4: Create the ambient type declaration**

Create `web/src/vite-env.d.ts`:

```typescript
/// <reference types="vite/client" />

// Injected at build time by vite.config.ts `define` (short git sha, or 'dev').
declare const __BUILD_SHA__: string
```

- [ ] **Step 5: Add the `define` to vite config**

In `web/vite.config.ts`, add the import at the top and a `define` key. Final file:

```typescript
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'path'
import { execSync } from 'node:child_process'

function resolveBuildSha(): string {
  if (process.env.BUILD_SHA) return process.env.BUILD_SHA
  try {
    return execSync('git rev-parse --short HEAD').toString().trim()
  } catch {
    return 'dev'
  }
}

export default defineConfig({
  plugins: [react(), tailwindcss()],
  define: {
    __BUILD_SHA__: JSON.stringify(resolveBuildSha()),
  },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8787',
        changeOrigin: true,
      },
      '/ws': {
        target: 'ws://127.0.0.1:8787',
        ws: true,
      },
    },
  },
})
```

- [ ] **Step 6: Run tests + type check**

Run: `cd web && ./node_modules/.bin/vitest run src/lib/version.test.ts && ./node_modules/.bin/tsc -b --force`
Expected: PASS + clean types (the `define` makes `__BUILD_SHA__` resolvable; the `.d.ts` types it).

- [ ] **Step 7: Lint + commit**

```bash
cd web && ./node_modules/.bin/eslint src/lib/version.ts src/lib/version.test.ts --max-warnings 0
cd .. && git add web/vite.config.ts web/src/vite-env.d.ts web/src/lib/version.ts web/src/lib/version.test.ts
git commit -m "feat(web): inject __BUILD_SHA__ + version-compare helpers

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: `useVersionWatch` hook

On each WebSocket reconnect, fetch `/healthz` and flip a sticky `updateAvailable` when the running
sha differs from `__BUILD_SHA__`.

**Files:**
- Create: `web/src/hooks/useVersionWatch.ts`
- Test: `web/src/hooks/useVersionWatch.test.ts`

**Interfaces:**
- Consumes: `getSessionSocket()`, `connectionStateChange` event, `isOutdated` (Task 1), `__BUILD_SHA__`.
- Produces: `useVersionWatch(opts?: { enabled?: boolean; buildSha?: string }): boolean` (the sticky `updateAvailable`).

- [ ] **Step 1: Write the failing test**

Create `web/src/hooks/useVersionWatch.test.ts`:

```typescript
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { useVersionWatch } from './useVersionWatch'
import * as wsClient from '@/ws/client'

// Minimal fake socket exposing the on/off event API.
function makeFakeSocket() {
  const handlers: Record<string, Set<(...a: unknown[]) => void>> = {}
  return {
    on: (e: string, cb: (...a: unknown[]) => void) => { (handlers[e] ??= new Set()).add(cb) },
    off: (e: string, cb: (...a: unknown[]) => void) => { handlers[e]?.delete(cb) },
    emit: (e: string, ...a: unknown[]) => { handlers[e]?.forEach((cb) => cb(...a)) },
  }
}

beforeEach(() => { vi.restoreAllMocks() })
afterEach(() => { vi.unstubAllGlobals() })

describe('useVersionWatch', () => {
  it('flips updateAvailable when /healthz reports a different sha on reconnect', async () => {
    const fake = makeFakeSocket()
    vi.spyOn(wsClient, 'getSessionSocket').mockReturnValue(fake as unknown as ReturnType<typeof wsClient.getSessionSocket>)
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ sha: 'newsha1' }) })))

    const { result } = renderHook(() => useVersionWatch({ enabled: true, buildSha: 'oldsha0' }))
    expect(result.current).toBe(false)

    fake.emit('connectionStateChange', 'connected')
    await waitFor(() => expect(result.current).toBe(true))
  })

  it('stays false when sha matches', async () => {
    const fake = makeFakeSocket()
    vi.spyOn(wsClient, 'getSessionSocket').mockReturnValue(fake as unknown as ReturnType<typeof wsClient.getSessionSocket>)
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ sha: 'samesha' }) })))

    const { result } = renderHook(() => useVersionWatch({ enabled: true, buildSha: 'samesha' }))
    fake.emit('connectionStateChange', 'connected')
    await new Promise((r) => setTimeout(r, 10))
    expect(result.current).toBe(false)
  })

  it('does nothing when disabled (dev/test default)', async () => {
    const fake = makeFakeSocket()
    vi.spyOn(wsClient, 'getSessionSocket').mockReturnValue(fake as unknown as ReturnType<typeof wsClient.getSessionSocket>)
    const fetchSpy = vi.fn()
    vi.stubGlobal('fetch', fetchSpy)

    renderHook(() => useVersionWatch({ enabled: false, buildSha: 'oldsha0' }))
    fake.emit('connectionStateChange', 'connected')
    await new Promise((r) => setTimeout(r, 10))
    expect(fetchSpy).not.toHaveBeenCalled()
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && ./node_modules/.bin/vitest run src/hooks/useVersionWatch.test.ts`
Expected: FAIL — `./useVersionWatch` not found.

- [ ] **Step 3: Create the hook**

Create `web/src/hooks/useVersionWatch.ts`:

```typescript
import { useEffect, useState } from 'react'
import { getSessionSocket, type ConnectionState } from '@/ws/client'
import { isOutdated, BUILD_SHA } from '@/lib/version'

interface Options {
  enabled?: boolean
  buildSha?: string
}

/**
 * Watches for a newer deployed frontend. On every WebSocket reconnect (a deploy always restarts
 * the edge → reconnect), fetches /healthz and compares the running sha to this bundle's build sha.
 * Returns a sticky `updateAvailable`. Reconnect-triggered only; gated to production so dev/test
 * never fetch or flip. `enabled`/`buildSha` are seams for testing.
 */
export function useVersionWatch(opts: Options = {}): boolean {
  const enabled = opts.enabled ?? import.meta.env.PROD
  const buildSha = opts.buildSha ?? BUILD_SHA
  const [updateAvailable, setUpdateAvailable] = useState(false)

  useEffect(() => {
    if (!enabled) return
    let socket: ReturnType<typeof getSessionSocket>
    try {
      socket = getSessionSocket()
    } catch {
      return // jsdom rejects '/ws'; no socket → nothing to watch
    }
    const check = async () => {
      try {
        const res = await fetch('/healthz')
        if (!res.ok) return
        const body = (await res.json()) as { sha?: string }
        if (isOutdated(buildSha, body.sha)) setUpdateAvailable(true)
      } catch {
        // unreachable / parse error — re-check on the next reconnect
      }
    }
    const onState = (state: ConnectionState) => { if (state === 'connected') void check() }
    socket.on('connectionStateChange', onState)
    return () => socket.off('connectionStateChange', onState)
  }, [enabled, buildSha])

  return updateAvailable
}
```

- [ ] **Step 4: Run tests + type check**

Run: `cd web && ./node_modules/.bin/vitest run src/hooks/useVersionWatch.test.ts && ./node_modules/.bin/tsc -b --force`
Expected: PASS + clean.

- [ ] **Step 5: Lint + commit**

```bash
cd web && ./node_modules/.bin/eslint src/hooks/useVersionWatch.ts src/hooks/useVersionWatch.test.ts --max-warnings 0
cd .. && git add web/src/hooks/useVersionWatch.ts web/src/hooks/useVersionWatch.test.ts
git commit -m "feat(web): useVersionWatch — detect a newer deploy on reconnect

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: `useAutoReload` hook

When an update is available, reload automatically the moment it's safe (focused session idle + no
buffered prompts). Re-evaluates as session status and buffered count change.

**Files:**
- Create: `web/src/hooks/useAutoReload.ts`
- Test: `web/src/hooks/useAutoReload.test.tsx`

**Interfaces:**
- Consumes: `useSessions()` (Task uses `web/src/api/hooks.ts`), `useUpdatePending()`, `getSessionSocket().activeSessionId`, `isSafeToReload` (Task 1).
- Produces: `useAutoReload(updateAvailable: boolean): void` (side effect: `location.reload()` when safe).

- [ ] **Step 1: Write the failing test**

Create `web/src/hooks/useAutoReload.test.tsx`:

```typescript
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import React from 'react'
import { useAutoReload } from './useAutoReload'
import * as hooks from '@/api/hooks'
import * as updatePending from '@/hooks/useUpdatePending'
import * as wsClient from '@/ws/client'

function Harness({ updateAvailable }: { updateAvailable: boolean }) {
  useAutoReload(updateAvailable)
  return null
}

function renderWith(updateAvailable: boolean) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <Harness updateAvailable={updateAvailable} />
    </QueryClientProvider>,
  )
}

const reload = vi.fn()
beforeEach(() => {
  reload.mockReset()
  vi.stubGlobal('location', { reload })
  vi.spyOn(wsClient, 'getSessionSocket').mockReturnValue(
    { activeSessionId: 's1', updatePendingState: { active: false, count: 0 } } as unknown as ReturnType<typeof wsClient.getSessionSocket>,
  )
})
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks() })

describe('useAutoReload', () => {
  it('reloads when update available + focused session idle + nothing buffered', () => {
    vi.spyOn(hooks, 'useSessions').mockReturnValue({ data: [{ id: 's1', status: 'idle' }] } as ReturnType<typeof hooks.useSessions>)
    vi.spyOn(updatePending, 'useUpdatePending').mockReturnValue({ active: false, count: 0 })
    renderWith(true)
    expect(reload).toHaveBeenCalledOnce()
  })

  it('does NOT reload while the focused session is mid-turn', () => {
    vi.spyOn(hooks, 'useSessions').mockReturnValue({ data: [{ id: 's1', status: 'in_progress' }] } as ReturnType<typeof hooks.useSessions>)
    vi.spyOn(updatePending, 'useUpdatePending').mockReturnValue({ active: false, count: 0 })
    renderWith(true)
    expect(reload).not.toHaveBeenCalled()
  })

  it('does NOT reload while prompts are buffered', () => {
    vi.spyOn(hooks, 'useSessions').mockReturnValue({ data: [{ id: 's1', status: 'idle' }] } as ReturnType<typeof hooks.useSessions>)
    vi.spyOn(updatePending, 'useUpdatePending').mockReturnValue({ active: true, count: 2 })
    renderWith(true)
    expect(reload).not.toHaveBeenCalled()
  })

  it('does NOT reload when no update is available', () => {
    vi.spyOn(hooks, 'useSessions').mockReturnValue({ data: [{ id: 's1', status: 'idle' }] } as ReturnType<typeof hooks.useSessions>)
    vi.spyOn(updatePending, 'useUpdatePending').mockReturnValue({ active: false, count: 0 })
    renderWith(false)
    expect(reload).not.toHaveBeenCalled()
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && ./node_modules/.bin/vitest run src/hooks/useAutoReload.test.tsx`
Expected: FAIL — `./useAutoReload` not found.

- [ ] **Step 3: Create the hook**

Create `web/src/hooks/useAutoReload.ts`:

```typescript
import { useEffect } from 'react'
import { useSessions } from '@/api/hooks'
import { useUpdatePending } from '@/hooks/useUpdatePending'
import { getSessionSocket } from '@/ws/client'
import { isSafeToReload } from '@/lib/version'

/**
 * Silently reloads to the new bundle once it's safe: an update is available, the focused session
 * (the one the socket is attached to) is not mid-turn, and no prompts are client-buffered. Unsent
 * composer drafts survive a reload via localStorage, so they are not a gate. Re-evaluates whenever
 * the session list (status) or the buffered-prompt count changes.
 */
export function useAutoReload(updateAvailable: boolean): void {
  const { data: sessions } = useSessions()
  const pending = useUpdatePending()

  useEffect(() => {
    if (!updateAvailable) return
    let activeId: string | null = null
    try {
      activeId = getSessionSocket().activeSessionId
    } catch {
      activeId = null // jsdom: no socket → treat as no focused session
    }
    const focused = activeId ? sessions?.find((s) => s.id === activeId) : undefined
    if (isSafeToReload(focused?.status, pending.count)) {
      location.reload()
    }
  }, [updateAvailable, sessions, pending.count])
}
```

- [ ] **Step 4: Run tests + type check**

Run: `cd web && ./node_modules/.bin/vitest run src/hooks/useAutoReload.test.tsx && ./node_modules/.bin/tsc -b --force`
Expected: PASS + clean.

- [ ] **Step 5: Lint + commit**

```bash
cd web && ./node_modules/.bin/eslint src/hooks/useAutoReload.ts src/hooks/useAutoReload.test.tsx --max-warnings 0
cd .. && git add web/src/hooks/useAutoReload.ts web/src/hooks/useAutoReload.test.tsx
git commit -m "feat(web): useAutoReload — silent reload when safe

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Banner + global mount in App

A `NewVersionBanner` (manual reload button) and a `GlobalUpdateBanner` that wires the two hooks,
mounted in `App` above both layouts so it shows on every route.

**Files:**
- Create: `web/src/components/NewVersionBanner.tsx`
- Create: `web/src/components/GlobalUpdateBanner.tsx`
- Modify: `web/src/App.tsx` (render `<GlobalUpdateBanner />` above the `isWide` branch)
- Test: `web/src/components/__tests__/NewVersionBanner.test.tsx`

**Interfaces:**
- Consumes: `useVersionWatch` (Task 2), `useAutoReload` (Task 3).
- Produces: `<NewVersionBanner onReload={() => void} />`; `<GlobalUpdateBanner />`.

- [ ] **Step 1: Write the failing test**

Create `web/src/components/__tests__/NewVersionBanner.test.tsx`:

```typescript
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { NewVersionBanner } from '../NewVersionBanner'

describe('NewVersionBanner', () => {
  it('announces a new version', () => {
    render(<NewVersionBanner onReload={vi.fn()} />)
    expect(screen.getByText(/new version available/i)).toBeInTheDocument()
  })

  it('calls onReload when the button is clicked', async () => {
    const onReload = vi.fn()
    const user = userEvent.setup()
    render(<NewVersionBanner onReload={onReload} />)
    await user.click(screen.getByRole('button', { name: /reload/i }))
    expect(onReload).toHaveBeenCalledOnce()
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && ./node_modules/.bin/vitest run src/components/__tests__/NewVersionBanner.test.tsx`
Expected: FAIL — module not found.

- [ ] **Step 3: Create the banner**

Create `web/src/components/NewVersionBanner.tsx` (mirrors `UpdatePendingBanner`, sky palette, fixed overlay, with an action button):

```typescript
import { RefreshCw } from 'lucide-react'
import { cn } from '@/lib/utils'

interface NewVersionBannerProps {
  onReload: () => void
}

export function NewVersionBanner({ onReload }: NewVersionBannerProps) {
  return (
    <div
      role="status"
      className={cn(
        'fixed inset-x-0 top-0 z-50 flex items-center justify-center gap-3',
        'border-b border-sky-300 dark:border-sky-700 bg-sky-50 dark:bg-sky-900/90 px-4 py-2',
      )}
    >
      <RefreshCw size={16} className="text-sky-700 dark:text-sky-300 flex-shrink-0" aria-hidden="true" />
      <span className="text-sm font-medium text-sky-900 dark:text-sky-100">
        New version available
      </span>
      <button
        type="button"
        onClick={onReload}
        className={cn(
          'rounded-md bg-sky-600 px-3 py-1 text-xs font-semibold text-white',
          'hover:bg-sky-700 focus:outline-none focus:ring-2 focus:ring-sky-400',
        )}
      >
        Reload
      </button>
    </div>
  )
}
```

- [ ] **Step 4: Create the global wiring component**

Create `web/src/components/GlobalUpdateBanner.tsx`:

```typescript
import { useVersionWatch } from '@/hooks/useVersionWatch'
import { useAutoReload } from '@/hooks/useAutoReload'
import { NewVersionBanner } from './NewVersionBanner'

/**
 * App-level glue: detect a newer deploy, auto-reload when safe, and otherwise surface a banner
 * with a manual Reload. Renders nothing until an update is available. Mounted once in App above
 * both the desktop and mobile layouts so it appears on every route.
 */
export function GlobalUpdateBanner() {
  const updateAvailable = useVersionWatch()
  useAutoReload(updateAvailable)
  if (!updateAvailable) return null
  return <NewVersionBanner onReload={() => location.reload()} />
}
```

- [ ] **Step 5: Mount in App**

In `web/src/App.tsx`, add the import and wrap the `App()` return so the banner sits above both
branches. Replace the body of `function App()` (the `isWide` branch + `<Routes>`):

```typescript
import { GlobalUpdateBanner } from './components/GlobalUpdateBanner'
```

```typescript
function App() {
  // Wide screens get the master-detail DesktopShell; narrow screens keep the
  // routed full-screen views below. useMediaQuery returns false when matchMedia
  // is unavailable (jsdom), so tests default to the mobile/routed branch.
  const isWide = useMediaQuery('(min-width: 1024px)')
  return (
    <>
      <GlobalUpdateBanner />
      {isWide ? (
        <DesktopShell />
      ) : (
        <Routes>
          <Route path="/" element={<Navigate to="/sessions" replace />} />
          <Route path="/sessions" element={<DashboardRoute tab="sessions" />} />
          <Route path="/accounts" element={<DashboardRoute tab="accounts" />} />
          <Route path="/archived" element={<DashboardRoute tab="archived" />} />
          <Route path="/session/:sessionId" element={<SessionRoute />} />
          <Route path="/session/:sessionId/subagent/:agentId" element={<SubSessionRoute />} />
          <Route path="/review/:sessionId" element={<ReviewRoute />} />
          <Route path="*" element={<Navigate to="/sessions" replace />} />
        </Routes>
      )}
    </>
  )
}
```

(In test/dev, `useVersionWatch` is gated off → `GlobalUpdateBanner` renders null, so existing App
behavior is unchanged.)

- [ ] **Step 6: Run the new test, the App suite, and the broad suite**

Run: `cd web && ./node_modules/.bin/vitest run src/components/__tests__/NewVersionBanner.test.tsx src/App.test.tsx`
Then the BROAD suite (App.tsx changes can break sibling tests via mock factories — only vitest catches it):
Run: `cd web && ./node_modules/.bin/vitest run`
Expected: all PASS, no NEW failures vs. baseline. If a pre-existing test was already failing for
unrelated reasons (see memory: some react-router-dom/@novnc tests are CI-only in the offline
worktree), note it; do not claim you broke it.

- [ ] **Step 7: Type, lint, commit**

```bash
cd web && ./node_modules/.bin/tsc -b --force && ./node_modules/.bin/eslint src/components/NewVersionBanner.tsx src/components/GlobalUpdateBanner.tsx src/App.tsx src/components/__tests__/NewVersionBanner.test.tsx --max-warnings 0
cd .. && git add web/src/components/NewVersionBanner.tsx web/src/components/GlobalUpdateBanner.tsx web/src/App.tsx web/src/components/__tests__/NewVersionBanner.test.tsx
git commit -m "feat(web): global new-version banner + auto-reload wiring

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Final verification (before PR)

- [ ] `cd web && ./node_modules/.bin/tsc -b --force` clean.
- [ ] `cd web && ./node_modules/.bin/eslint src --max-warnings 0` clean.
- [ ] `cd web && ./node_modules/.bin/vitest run` — green (no NEW failures).
- [ ] Sanity: `cd web && BUILD_SHA=testsha123 ./node_modules/.bin/vite build` then confirm `grep -rl testsha123 dist/assets/*.js` finds the injected sha (proves the define reaches the bundle). Then remove `dist` if it's git-ignored noise.
- [ ] Open a non-draft PR; arm `gh pr merge <#> --squash --auto`.

## Spec coverage check

- Build-SHA injection → Task 1 (`vite.config.ts` define + `vite-env.d.ts`).
- Reconnect-triggered detection, PROD-gated, `/healthz` compare → Task 2 (`useVersionWatch`).
- Conservative safety bar (focused session not mid-turn + no buffered prompts; drafts survive) → Task 1 `isSafeToReload` + Task 3 `useAutoReload`.
- Hybrid reaction (banner + auto-reload) → Task 4 (`NewVersionBanner` + `GlobalUpdateBanner`) + Task 3.
- Global mount (visible on every route, not Dashboard-only) → Task 4 (`App.tsx`, above both layouts).
- Edge cases (dev gate, unreachable /healthz, rollback, no reload loop via sticky+sha-match, stacking with drain banner) → Tasks 2-3 (`enabled` gate, try/catch, sticky state, buffered-count gate).
- Testing (version watch, safety predicate, banner) → Tasks 1-4 vitest.
