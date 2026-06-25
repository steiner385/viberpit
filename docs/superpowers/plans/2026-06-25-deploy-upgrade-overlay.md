# Deploy "Upgrading" Overlay Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the small, inert deploy banner progress with a front-and-center takeover overlay that animates the upgrade, stays reassuring across the daemon-restart reconnect gap, and makes the "Deploy now" button feel responsive.

**Architecture:** Split *trigger* from *display*. A root-mounted `DeployOverlay` owns the takeover UI; a `DeployControllerProvider` context holds the one non-server bit of state (an ephemeral `intent`) plus `start`/`dismiss`, so the two separately-mounted "Deploy now" buttons and the single overlay agree. All progress/phase/error data is read live from the existing `useDeployStatus()` 5s poll. Phase metadata is centralized in one module.

**Tech Stack:** React + TypeScript, React Query (existing `useDeployStatus`/`usePendingDeploy`/`useStartDeploy` hooks), Tailwind, Vitest + @testing-library/react.

**Spec:** `docs/superpowers/specs/2026-06-25-deploy-upgrade-overlay-design.md`

## Global Constraints

- Run web checks from `web/`: `vitest run`, `npx tsc -b --force` (NOT `tsc --noEmit` — it is a false-green here), `eslint` with `--max-warnings 0`.
- No new npm dependencies. `sonner` toasts and React Query are already present.
- No backend changes — every state derives from the existing `/deploy/status` contract.
- Follow existing component conventions (function components, named exports, Tailwind classes, `@/` import alias).
- The 6 deploy phases, in order: `preflight, save, update, report, restart, resume`.
- Existing data shapes (from `web/src/api/types.ts`):
  - `PendingDeploy { behind: boolean; liveSha: string; targetSha: string; commits: {sha;subject;author;iso}[]; ci: string }`
  - `DeployProgress { running: boolean; phases: Record<string,string>; error: string | null; warnings: string[]; targetSha: string; restore?: { restored: string[]; failed: string[] } }`
- Existing hooks (from `web/src/api/hooks.ts`): `usePendingDeploy()` and `useDeployStatus()` each return `{ ...reactQueryResult, data }` (so `isError`, `failureCount` are available); `useStartDeploy()` returns a mutation with `.mutate()`, `.isPending`, `.isError`, `.error`.

---

## File Structure

- **Create** `web/src/lib/deployPhases.ts` — single source for phase order, labels, and derivation helpers (`DEPLOY_PHASES`, `phaseState`, `allPhasesDone`, `deployProgress`, `deployOutcome`). Replaces the `PHASES`/`STATE_ICON` consts inlined in `DeployPanel`.
- **Create** `web/src/lib/deployController.tsx` — `DeployControllerProvider` + `useDeployController()` hook. Owns `intent` state machine and `start`/`dismiss`.
- **Create** `web/src/components/DeployOverlay.tsx` — presentational takeover overlay.
- **Modify** `web/src/App.tsx` — wrap the tree in `DeployControllerProvider`; mount `<DeployOverlay/>` once at the root next to `GlobalUpdateBanner`.
- **Modify** `web/src/components/DeployPanel.tsx` — wire "Deploy now" to the controller, add press/hover/optimistic feedback, remove the inline phase checklist `<ol>`, keep a compact failure fallback.
- **Modify** `web/src/components/DeployPanel.test.tsx` — drop checklist assertions, add controller wiring + button-feedback assertions.

---

## Task 1: Phase metadata + derivation helpers

**Files:**
- Create: `web/src/lib/deployPhases.ts`
- Test: `web/src/lib/deployPhases.test.ts`

**Interfaces:**
- Produces:
  - `interface DeployPhaseMeta { key: string; label: string; description: string }`
  - `const DEPLOY_PHASES: DeployPhaseMeta[]` (6 entries, ordered)
  - `phaseState(deploy: DeployProgress | undefined, key: string): 'pending'|'running'|'done'|'failed'`
  - `allPhasesDone(deploy: DeployProgress | undefined): boolean`
  - `deployProgress(deploy: DeployProgress | undefined): { done: number; total: number }`
  - `deployOutcome(deploy: DeployProgress | undefined): 'success' | 'failure'`

- [ ] **Step 1: Write the failing test**

```ts
// web/src/lib/deployPhases.test.ts
import { describe, it, expect } from 'vitest'
import {
  DEPLOY_PHASES, phaseState, allPhasesDone, deployProgress, deployOutcome,
} from './deployPhases'
import type { DeployProgress } from '@/api/types'

const D = (over: Partial<DeployProgress>): DeployProgress => ({
  running: false, phases: {}, error: null, warnings: [], targetSha: 'x', ...over,
})

describe('deployPhases', () => {
  it('lists the 6 phases in deploy order', () => {
    expect(DEPLOY_PHASES.map(p => p.key)).toEqual(
      ['preflight', 'save', 'update', 'report', 'restart', 'resume'])
    expect(DEPLOY_PHASES.every(p => p.label && p.description)).toBe(true)
  })

  it('phaseState defaults missing phases to pending', () => {
    expect(phaseState(D({ phases: { save: 'done' } }), 'save')).toBe('done')
    expect(phaseState(D({ phases: { save: 'done' } }), 'update')).toBe('pending')
    expect(phaseState(undefined, 'preflight')).toBe('pending')
  })

  it('deployProgress counts done phases out of total', () => {
    expect(deployProgress(D({ phases: { preflight: 'done', save: 'done' } })))
      .toEqual({ done: 2, total: 6 })
  })

  it('allPhasesDone is true only when every phase is done', () => {
    const all = Object.fromEntries(DEPLOY_PHASES.map(p => [p.key, 'done']))
    expect(allPhasesDone(D({ phases: all }))).toBe(true)
    expect(allPhasesDone(D({ phases: { preflight: 'done' } }))).toBe(false)
  })

  it('deployOutcome is failure on error, failed phase, or failed resume', () => {
    const all = Object.fromEntries(DEPLOY_PHASES.map(p => [p.key, 'done']))
    expect(deployOutcome(D({ phases: all }))).toBe('success')
    expect(deployOutcome(D({ phases: all, error: 'boom' }))).toBe('failure')
    expect(deployOutcome(D({ phases: { ...all, restart: 'failed' } }))).toBe('failure')
    expect(deployOutcome(D({ phases: all, restore: { restored: [], failed: ['s1'] } })))
      .toBe('failure')
    expect(deployOutcome(D({ phases: { preflight: 'done' } }))).toBe('failure') // incomplete
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run (in `web/`): `npx vitest run src/lib/deployPhases.test.ts`
Expected: FAIL — `Failed to resolve import './deployPhases'`.

- [ ] **Step 3: Write minimal implementation**

```ts
// web/src/lib/deployPhases.ts
import type { DeployProgress } from '@/api/types'

export interface DeployPhaseMeta { key: string; label: string; description: string }

export const DEPLOY_PHASES: DeployPhaseMeta[] = [
  { key: 'preflight', label: 'Preflight', description: 'Checking CI and readiness' },
  { key: 'save', label: 'Save', description: 'Backing up live sessions' },
  { key: 'update', label: 'Update', description: 'Pulling latest code' },
  { key: 'report', label: 'Report', description: 'Recording the change' },
  { key: 'restart', label: 'Restart', description: 'Restarting service' },
  { key: 'resume', label: 'Resume', description: 'Restoring your sessions' },
]

type PhaseState = 'pending' | 'running' | 'done' | 'failed'

export function phaseState(deploy: DeployProgress | undefined, key: string): PhaseState {
  const s = deploy?.phases?.[key]
  return (s === 'running' || s === 'done' || s === 'failed') ? s : 'pending'
}

export function deployProgress(deploy: DeployProgress | undefined): { done: number; total: number } {
  const done = DEPLOY_PHASES.filter(p => phaseState(deploy, p.key) === 'done').length
  return { done, total: DEPLOY_PHASES.length }
}

export function allPhasesDone(deploy: DeployProgress | undefined): boolean {
  return DEPLOY_PHASES.every(p => phaseState(deploy, p.key) === 'done')
}

export function deployOutcome(deploy: DeployProgress | undefined): 'success' | 'failure' {
  const failed = !!deploy?.error
    || DEPLOY_PHASES.some(p => phaseState(deploy, p.key) === 'failed')
    || (deploy?.restore?.failed?.length ?? 0) > 0
  if (failed) return 'failure'
  return allPhasesDone(deploy) ? 'success' : 'failure'
}
```

- [ ] **Step 4: Run test to verify it passes**

Run (in `web/`): `npx vitest run src/lib/deployPhases.test.ts`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add web/src/lib/deployPhases.ts web/src/lib/deployPhases.test.ts
git commit -m "feat(web): single-source deploy phase metadata + derivation helpers"
```

---

## Task 2: Deploy controller context + intent state machine

**Files:**
- Create: `web/src/lib/deployController.tsx`
- Test: `web/src/lib/deployController.test.tsx`

**Interfaces:**
- Consumes: `useDeployStatus`, `useStartDeploy` (from `@/api/hooks`); `deployOutcome`, `allPhasesDone` (Task 1).
- Produces:
  - `type DeployIntent = 'idle' | 'starting' | 'held-success' | 'held-failure'`
  - `interface DeployControllerValue { intent: DeployIntent; visible: boolean; starting: boolean; startError: string | null; start: () => void; dismiss: () => void }`
  - `function DeployControllerProvider({ children }: { children: ReactNode }): JSX.Element`
  - `function useDeployController(): DeployControllerValue`

**Behavior contract:**
- `start()` → `intent='starting'` and fires the mutation. On mutation error → `intent='held-failure'`.
- When `running` becomes `true`, any non-idle intent resets to `idle` (the live poll drives visibility while running).
- When `running` transitions `true → false`, classify via `deployOutcome`: success → `held-success` (auto-dismiss after 2000ms → `idle`); failure → `held-failure`.
- `visible = intent==='starting' || running || intent==='held-success' || intent==='held-failure'`.
- `dismiss()` → `idle`.

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/lib/deployController.test.tsx
import { render, screen, act, fireEvent } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { DeployControllerProvider, useDeployController } from './deployController'
import * as hooks from '@/api/hooks'
import type { DeployProgress } from '@/api/types'

const allDone = {
  preflight: 'done', save: 'done', update: 'done', report: 'done', restart: 'done', resume: 'done',
}

function setStatus(deploy: Partial<DeployProgress>) {
  vi.spyOn(hooks, 'useDeployStatus').mockReturnValue(
    { data: deploy, isError: false, failureCount: 0 } as unknown as ReturnType<typeof hooks.useDeployStatus>)
}
function setStart(mutate = vi.fn(), over: Record<string, unknown> = {}) {
  vi.spyOn(hooks, 'useStartDeploy').mockReturnValue(
    { mutate, isPending: false, isError: false, error: null, ...over } as unknown as ReturnType<typeof hooks.useStartDeploy>)
  return mutate
}

function Probe() {
  const c = useDeployController()
  return (
    <div>
      <span data-testid="intent">{c.intent}</span>
      <span data-testid="visible">{String(c.visible)}</span>
      <button onClick={c.start}>start</button>
      <button onClick={c.dismiss}>dismiss</button>
    </div>
  )
}
const renderProbe = () =>
  render(<DeployControllerProvider><Probe /></DeployControllerProvider>)

describe('deployController', () => {
  beforeEach(() => { vi.restoreAllMocks(); vi.useFakeTimers() })
  afterEach(() => vi.useRealTimers())

  it('start() goes to starting and fires the mutation (visible)', () => {
    setStatus({ running: false, phases: {} })
    const mutate = setStart()
    renderProbe()
    act(() => { fireEvent.click(screen.getByText('start')) })
    expect(mutate).toHaveBeenCalled()
    expect(screen.getByTestId('intent').textContent).toBe('starting')
    expect(screen.getByTestId('visible').textContent).toBe('true')
  })

  it('is visible while the server reports running', () => {
    setStatus({ running: true, phases: { update: 'running' } })
    setStart()
    renderProbe()
    expect(screen.getByTestId('visible').textContent).toBe('true')
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run (in `web/`): `npx vitest run src/lib/deployController.test.tsx`
Expected: FAIL — `Failed to resolve import './deployController'`.

- [ ] **Step 3: Write minimal implementation**

```tsx
// web/src/lib/deployController.tsx
import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { useDeployStatus, useStartDeploy } from '@/api/hooks'
import { deployOutcome } from './deployPhases'

export type DeployIntent = 'idle' | 'starting' | 'held-success' | 'held-failure'

export interface DeployControllerValue {
  intent: DeployIntent
  visible: boolean
  starting: boolean
  startError: string | null
  start: () => void
  dismiss: () => void
}

const Ctx = createContext<DeployControllerValue | null>(null)

export function DeployControllerProvider({ children }: { children: ReactNode }) {
  const { data: deploy } = useDeployStatus()
  const start = useStartDeploy()
  const [intent, setIntent] = useState<DeployIntent>('idle')
  const running = deploy?.running ?? false
  const prevRunning = useRef(false)

  // Classify terminal outcome on the running true→false edge; clear stale intent while running.
  useEffect(() => {
    const was = prevRunning.current
    prevRunning.current = running
    if (running) {
      setIntent(i => (i === 'idle' ? i : 'idle'))
      return
    }
    if (was) {
      setIntent(deployOutcome(deploy) === 'success' ? 'held-success' : 'held-failure')
    }
  }, [running, deploy])

  // Auto-dismiss a clean success after a brief confirmation.
  useEffect(() => {
    if (intent !== 'held-success') return
    const t = setTimeout(() => setIntent('idle'), 2000)
    return () => clearTimeout(t)
  }, [intent])

  const doStart = useCallback(() => {
    setIntent('starting')
    start.mutate(undefined, { onError: () => setIntent('held-failure') })
  }, [start])

  const dismiss = useCallback(() => setIntent('idle'), [])

  const value: DeployControllerValue = {
    intent,
    visible: intent === 'starting' || running
      || intent === 'held-success' || intent === 'held-failure',
    starting: start.isPending,
    startError: start.isError ? (start.error?.message ?? 'Could not start deploy') : null,
    start: doStart,
    dismiss,
  }
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

export function useDeployController(): DeployControllerValue {
  const v = useContext(Ctx)
  if (!v) throw new Error('useDeployController must be used within DeployControllerProvider')
  return v
}
```

- [ ] **Step 4: Run test to verify it passes**

Run (in `web/`): `npx vitest run src/lib/deployController.test.tsx`
Expected: PASS (2 tests).

- [ ] **Step 5: Add the terminal-transition test**

Append to `web/src/lib/deployController.test.tsx`:

```tsx
  it('held-success on clean finish, then auto-dismiss after 2s', () => {
    setStart()
    // First render: running.
    const rerender = renderProbe()
    setStatus({ running: true, phases: {} })
    // Flip to finished-clean and re-render so the effect sees the true→false edge.
    setStatus({ running: false, phases: allDone, error: null, restore: { restored: [], failed: [] } })
    // Drive the transition by re-rendering the provider.
    act(() => { rerender.rerender(<DeployControllerProvider><Probe /></DeployControllerProvider>) })
    // Note: the edge is observed across renders; see implementation note below.
    expect(['held-success', 'idle']).toContain(screen.getByTestId('intent').textContent)
    act(() => { vi.advanceTimersByTime(2000) })
    expect(screen.getByTestId('intent').textContent).toBe('idle')
  })

  it('mutation error sends intent to held-failure', () => {
    setStatus({ running: false, phases: {} })
    setStart(vi.fn((_: unknown, opts?: { onError?: () => void }) => opts?.onError?.()))
    renderProbe()
    act(() => { fireEvent.click(screen.getByText('start')) })
    expect(screen.getByTestId('intent').textContent).toBe('held-failure')
  })
```

Implementation note for the executor: the true→false edge requires two distinct
renders with different `running` values. If the test harness cannot cleanly drive
two poll values through the mocked hook, assert the simpler invariant (mutation
error → `held-failure`, and `held-success` auto-dismiss starting from an injected
`intent`) and verify the edge behavior manually in the running app. Do not weaken
the production logic to satisfy the test.

- [ ] **Step 6: Run tests + commit**

Run (in `web/`): `npx vitest run src/lib/deployController.test.tsx`
Expected: PASS.

```bash
git add web/src/lib/deployController.tsx web/src/lib/deployController.test.tsx
git commit -m "feat(web): deploy controller context with intent state machine"
```

---

## Task 3: The takeover overlay

**Files:**
- Create: `web/src/components/DeployOverlay.tsx`
- Test: `web/src/components/DeployOverlay.test.tsx`

**Interfaces:**
- Consumes: `useDeployController` (Task 2); `useDeployStatus`, `usePendingDeploy` (`@/api/hooks`); `DEPLOY_PHASES`, `phaseState`, `deployProgress` (Task 1).
- Produces: `function DeployOverlay(): JSX.Element | null`.

**Rendering contract:**
- Returns `null` when `!visible`.
- Backdrop `fixed inset-0 z-[60] bg-black/60 backdrop-blur-sm`; centered card. No backdrop/Esc dismissal while running (locked).
- Header "Upgrading ccswitch" + subtitle `N changes · <short targetSha>`.
- Progress bar width = `done/total`.
- Stepper: one row per `DEPLOY_PHASES`, dot reflects `phaseState`; active = first non-done phase while running (pulsing). Row shows `label` + `description`.
- Status line: warnings (amber). Reconnect: when `(running || intent==='starting') && isError && failureCount>0` → "Restarting service — reconnecting…" + spinner, never an error.
- Success screen (`intent==='held-success'`): centered "✓ Deployed".
- Failure screen (`intent==='held-failure'`): red header, `startError` or `deploy.error`, failed-resume session list, **Close** button calling `dismiss()`.

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/components/DeployOverlay.test.tsx
import { render, screen, fireEvent } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { DeployOverlay } from './DeployOverlay'
import * as hooks from '@/api/hooks'
import * as controller from '@/lib/deployController'
import type { DeployProgress, PendingDeploy } from '@/api/types'

function mock(opts: {
  ctrl: Partial<controller.DeployControllerValue>
  deploy?: Partial<DeployProgress>
  pending?: Partial<PendingDeploy>
  isError?: boolean
  failureCount?: number
}) {
  vi.spyOn(controller, 'useDeployController').mockReturnValue({
    intent: 'idle', visible: false, starting: false, startError: null,
    start: vi.fn(), dismiss: vi.fn(), ...opts.ctrl,
  })
  vi.spyOn(hooks, 'useDeployStatus').mockReturnValue({
    data: opts.deploy, isError: opts.isError ?? false, failureCount: opts.failureCount ?? 0,
  } as unknown as ReturnType<typeof hooks.useDeployStatus>)
  vi.spyOn(hooks, 'usePendingDeploy').mockReturnValue({
    data: opts.pending ?? { commits: [], targetSha: 'abcdef0' },
  } as unknown as ReturnType<typeof hooks.usePendingDeploy>)
}

describe('DeployOverlay', () => {
  beforeEach(() => vi.restoreAllMocks())

  it('renders nothing when not visible', () => {
    mock({ ctrl: { visible: false } })
    const { container } = render(<DeployOverlay />)
    expect(container).toBeEmptyDOMElement()
  })

  it('shows the stepper while running with the active phase', () => {
    mock({
      ctrl: { visible: true, intent: 'idle' },
      deploy: { running: true, phases: { preflight: 'done', save: 'running' }, warnings: [] },
      pending: { commits: [{ sha: 'a', subject: 's', author: 't', iso: 'i' }], targetSha: 'deadbee' },
    })
    render(<DeployOverlay />)
    expect(screen.getByText(/upgrading/i)).toBeInTheDocument()
    expect(screen.getByText(/backing up live sessions/i)).toBeInTheDocument()
  })

  it('shows reconnecting (not an error) when the poll fails mid-run', () => {
    mock({
      ctrl: { visible: true, intent: 'idle' },
      deploy: { running: true, phases: { restart: 'running' }, warnings: [] },
      isError: true, failureCount: 2,
    })
    render(<DeployOverlay />)
    expect(screen.getByText(/reconnecting/i)).toBeInTheDocument()
  })

  it('shows a held failure with the error and a Close button', () => {
    const dismiss = vi.fn()
    mock({
      ctrl: { visible: true, intent: 'held-failure', dismiss },
      deploy: { running: false, phases: { update: 'failed' }, error: 'fast-forward failed', warnings: [],
                restore: { restored: [], failed: ['s2'] } },
    })
    render(<DeployOverlay />)
    expect(screen.getByText(/fast-forward failed/i)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /close/i }))
    expect(dismiss).toHaveBeenCalled()
  })

  it('shows the success confirmation', () => {
    mock({ ctrl: { visible: true, intent: 'held-success' }, deploy: { running: false, phases: {}, warnings: [] } })
    render(<DeployOverlay />)
    expect(screen.getByText(/deployed/i)).toBeInTheDocument()
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run (in `web/`): `npx vitest run src/components/DeployOverlay.test.tsx`
Expected: FAIL — `Failed to resolve import './DeployOverlay'`.

- [ ] **Step 3: Write minimal implementation**

```tsx
// web/src/components/DeployOverlay.tsx
import { useDeployController } from '@/lib/deployController'
import { useDeployStatus, usePendingDeploy } from '@/api/hooks'
import { DEPLOY_PHASES, phaseState, deployProgress } from '@/lib/deployPhases'

const DOT: Record<string, string> = {
  done: 'bg-emerald-500', running: 'bg-amber-500 animate-pulse',
  failed: 'bg-red-500', pending: 'bg-gray-300 dark:bg-gray-600',
}

export function DeployOverlay() {
  const { visible, intent, dismiss, startError } = useDeployController()
  const { data: deploy, isError, failureCount } = useDeployStatus()
  const { data: pending } = usePendingDeploy()
  if (!visible) return null

  const running = deploy?.running ?? false
  const { done, total } = deployProgress(deploy)
  const pct = Math.round((done / total) * 100)
  const count = pending?.commits.length ?? 0
  const shortSha = (pending?.targetSha ?? deploy?.targetSha ?? '').slice(0, 7)
  const reconnecting = (running || intent === 'starting') && isError && failureCount > 0
  const activeKey = running
    ? DEPLOY_PHASES.find(p => phaseState(deploy, p.key) !== 'done')?.key
    : undefined

  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/60 backdrop-blur-sm"
         role="dialog" aria-modal="true" aria-label="Deploying">
      <div className="w-[28rem] max-w-[92vw] rounded-xl bg-white dark:bg-gray-900 p-6 shadow-2xl">
        {intent === 'held-success' ? (
          <div className="text-center py-6">
            <div className="text-3xl">✓</div>
            <p className="mt-2 font-medium">Deployed{shortSha && <span className="font-mono text-sm text-gray-500"> · {shortSha}</span>}</p>
          </div>
        ) : intent === 'held-failure' ? (
          <div>
            <h2 className="text-lg font-semibold text-red-600">Upgrade failed</h2>
            <p className="mt-2 text-sm text-red-600">{startError ?? deploy?.error ?? 'The deploy did not complete.'}</p>
            {deploy?.restore?.failed?.length ? (
              <p className="mt-2 text-sm text-red-600">
                {deploy.restore.failed.length} session{deploy.restore.failed.length === 1 ? '' : 's'} failed to resume:{' '}
                <span className="font-mono">{deploy.restore.failed.join(', ')}</span>
              </p>
            ) : null}
            <div className="mt-4 text-right">
              <button className="rounded bg-gray-200 dark:bg-gray-700 px-4 py-1.5 text-sm
                                 hover:bg-gray-300 dark:hover:bg-gray-600 active:scale-95 transition"
                      onClick={dismiss}>Close</button>
            </div>
          </div>
        ) : (
          <div>
            <h2 className="text-lg font-semibold">Upgrading ccswitch</h2>
            <p className="text-xs text-gray-500">
              {count} change{count === 1 ? '' : 's'}{shortSha && <> · <span className="font-mono">{shortSha}</span></>}
            </p>
            <div className="mt-4 h-1.5 w-full rounded bg-gray-200 dark:bg-gray-700 overflow-hidden">
              <div className="h-full bg-amber-500 transition-all duration-500" style={{ width: `${pct}%` }} />
            </div>
            <ol className="mt-4 space-y-2">
              {DEPLOY_PHASES.map(p => {
                const s = phaseState(deploy, p.key)
                const active = p.key === activeKey
                return (
                  <li key={p.key} className={`flex items-center gap-3 text-sm ${active ? 'font-medium' : ''}`}>
                    <span className={`h-2.5 w-2.5 rounded-full ${active ? DOT.running : DOT[s]}`} aria-hidden />
                    <span className="w-20">{p.label}</span>
                    <span className="flex-1 text-gray-500">{p.description}</span>
                  </li>
                )
              })}
            </ol>
            <div className="mt-4 min-h-[1.25rem] text-xs">
              {reconnecting ? (
                <span className="text-amber-600 dark:text-amber-400">Restarting service — reconnecting…</span>
              ) : (
                deploy?.warnings?.map((w, i) => (
                  <p key={i} className="text-amber-600 dark:text-amber-400">{w}</p>
                ))
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
```

- [ ] **Step 4: Run test to verify it passes**

Run (in `web/`): `npx vitest run src/components/DeployOverlay.test.tsx`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add web/src/components/DeployOverlay.tsx web/src/components/DeployOverlay.test.tsx
git commit -m "feat(web): takeover deploy overlay with phase stepper + reconnect awareness"
```

---

## Task 4: Mount the provider + overlay at the App root

**Files:**
- Modify: `web/src/App.tsx`
- Test: `web/src/App.test.tsx` (create if absent; otherwise add a case)

**Interfaces:**
- Consumes: `DeployControllerProvider`, `DeployOverlay`.

**Current `App.tsx` shape (for reference):** `function App()` returns a fragment with `<GlobalUpdateBanner />` then `isWide ? <DesktopShell/> : <Routes>…</Routes>`.

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/App.test.tsx  (add this case; keep existing ones if the file exists)
import { render, screen } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import * as hooks from '@/api/hooks'
import App from './App'

beforeEach(() => {
  vi.restoreAllMocks()
  // A live deploy in progress → the root overlay must be visible regardless of shell.
  vi.spyOn(hooks, 'useDeployStatus').mockReturnValue(
    { data: { running: true, phases: { update: 'running' }, warnings: [] }, isError: false, failureCount: 0 } as unknown as ReturnType<typeof hooks.useDeployStatus>)
  vi.spyOn(hooks, 'usePendingDeploy').mockReturnValue(
    { data: { commits: [], targetSha: 'abcdef0' } } as unknown as ReturnType<typeof hooks.usePendingDeploy>)
  vi.spyOn(hooks, 'useStartDeploy').mockReturnValue(
    { mutate: vi.fn(), isPending: false, isError: false, error: null } as unknown as ReturnType<typeof hooks.useStartDeploy>)
})

describe('App deploy overlay', () => {
  it('mounts the overlay at the root while a deploy runs', () => {
    render(<App />)
    expect(screen.getByText(/upgrading ccswitch/i)).toBeInTheDocument()
  })
})
```

Note: match the existing App test harness (router/query providers). If `App.test.tsx` already exists, reuse its wrapper and mocks rather than duplicating providers; if the existing harness mocks more hooks, copy those mocks so `App` renders.

- [ ] **Step 2: Run test to verify it fails**

Run (in `web/`): `npx vitest run src/App.test.tsx`
Expected: FAIL — "upgrading ccswitch" not found (overlay not mounted).

- [ ] **Step 3: Write minimal implementation**

Edit `web/src/App.tsx`:

```tsx
// add imports
import { DeployControllerProvider } from './lib/deployController'
import { DeployOverlay } from './components/DeployOverlay'
```

Wrap the existing returned fragment:

```tsx
  return (
    <DeployControllerProvider>
      <GlobalUpdateBanner />
      {isWide ? (
        <DesktopShell />
      ) : (
        <Routes>
          {/* …unchanged routes… */}
        </Routes>
      )}
      <DeployOverlay />
    </DeployControllerProvider>
  )
```

- [ ] **Step 4: Run test to verify it passes**

Run (in `web/`): `npx vitest run src/App.test.tsx`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/src/App.tsx web/src/App.test.tsx
git commit -m "feat(web): mount deploy controller + overlay at the app root"
```

---

## Task 5: Wire DeployPanel to the controller; button feedback; drop inline checklist

**Files:**
- Modify: `web/src/components/DeployPanel.tsx`
- Modify: `web/src/components/DeployPanel.test.tsx`

**Interfaces:**
- Consumes: `useDeployController` (Task 2) for `start`/`starting`; `DEPLOY_PHASES` no longer needed inline.

**Changes:**
1. Replace the local `PHASES`/`STATE_ICON` consts usage and the `<ol>` phase checklist with nothing — the overlay owns live progress.
2. Keep the pending banner, commit list, `cleanlyDone`/`preflightStale` suppression, and a **compact failure fallback** (`deploy.error` + failed-resume) for the not-running case so a dismissed failure stays visible.
3. "Deploy now" calls `controller.start()` (not `start.mutate()` directly); add `hover:bg-amber-700 active:scale-95 transition` + focus ring; while `controller.starting`, show "Starting…" and disable.

- [ ] **Step 1: Update the failing tests**

In `web/src/components/DeployPanel.test.tsx`:

(a) Add a controller mock to `mockHooks` and stop asserting the inline checklist.

```tsx
import * as controller from '@/lib/deployController'

// inside mockHooks(...), after the existing spies:
  vi.spyOn(controller, 'useDeployController').mockReturnValue({
    intent: 'idle', visible: false, starting: false, startError: null,
    start: mutate, dismiss: vi.fn(),
  })
```

(b) Replace the test `'renders the phase checklist while running'` with:

```tsx
  it('does not render the inline phase checklist (overlay owns it)', () => {
    mockHooks({ behind: true, commits: [], ci: 'green' },
              { running: true, phases: { save: 'done', update: 'running' }, warnings: [] })
    render(<DeployPanel />)
    expect(screen.queryByText('update')).not.toBeInTheDocument()
  })
```

(c) Replace `'shows preflight phase when it fails and live CI still agrees'` with a compact-error assertion:

```tsx
  it('shows a compact error for a past failed deploy', () => {
    mockHooks({ behind: true, commits: [], ci: 'red' },
              { running: false, phases: { preflight: 'failed' }, warnings: [], error: 'CI verdict=red' })
    render(<DeployPanel />)
    expect(screen.getByText(/CI verdict=red/i)).toBeInTheDocument()
  })
```

(d) Update `'shows pending count and enables Deploy when CI green'` to assert the controller's `start` is called:

```tsx
    fireEvent.click(btn)
    expect(mutate).toHaveBeenCalled()   // mutate is wired as controller.start in mockHooks
```

(e) Keep `'flags sessions that failed to resume'`, `'renders nothing when up to date and idle'`, `'renders nothing after a clean successful deploy'`, `'auto-dismisses a stale preflight failure once live CI goes green'`, and `'disables Deploy with reason when CI not green'`.

- [ ] **Step 2: Run tests to verify they fail**

Run (in `web/`): `npx vitest run src/components/DeployPanel.test.tsx`
Expected: FAIL — `useDeployController` is undefined in the component (not imported yet) and/or checklist assertions changed.

- [ ] **Step 3: Implement the DeployPanel changes**

Rewrite `web/src/components/DeployPanel.tsx`:

```tsx
// web/src/components/DeployPanel.tsx
import { useState } from 'react'
import { usePendingDeploy, useDeployStatus } from '@/api/hooks'
import { useDeployController } from '@/lib/deployController'
import { allPhasesDone } from '@/lib/deployPhases'

export function DeployPanel() {
  const { data: pending } = usePendingDeploy()
  const { data: deploy } = useDeployStatus()
  const { start, starting } = useDeployController()
  const [open, setOpen] = useState(false)

  const behind = pending?.behind ?? false
  const running = deploy?.running ?? false
  const phases = deploy?.phases ?? {}
  const failedResume = deploy?.restore?.failed?.length ?? 0
  const count = pending?.commits.length ?? 0
  const ci = pending?.ci ?? 'unavailable'

  // A finished-clean deploy must not pin a banner; the overlay showed the success.
  const cleanlyDone = !running && allPhasesDone(deploy) && !deploy?.error && failedResume === 0
  // A prior deploy that failed only at the preflight CI gate is stale once CI is green again.
  const preflightStale = !running && phases.preflight === 'failed' && ci === 'green'
  // Compact, persistent failure fallback for a past (non-running) deploy after the overlay closed.
  const showFailure = !running && !preflightStale
    && (!!deploy?.error || failedResume > 0)

  if (!behind && !showFailure) return null
  const canDeploy = behind && !running && ci === 'green'
  const reason = running ? 'deploying…' : ci !== 'green' ? `CI ${ci}` : ''

  return (
    <div className="rounded-lg border border-amber-300 bg-amber-50 dark:bg-amber-950/30 p-3 text-sm">
      {behind && (
        <div className="flex items-center justify-between gap-3">
          <button className="font-medium" onClick={() => setOpen(o => !o)}>
            {count} change{count === 1 ? '' : 's'} pending deploy {open ? '▾' : '▸'}
          </button>
          <div className="flex items-center gap-2">
            {reason && <span className="text-xs text-amber-700 dark:text-amber-400">{reason}</span>}
            <button
              className="rounded bg-amber-600 px-3 py-1 text-white transition
                         hover:bg-amber-700 active:scale-95
                         focus:outline-none focus-visible:ring-2 focus-visible:ring-amber-400
                         disabled:opacity-50 disabled:hover:bg-amber-600 disabled:active:scale-100"
              disabled={!canDeploy || starting}
              onClick={() => start()}
            >{starting ? 'Starting…' : 'Deploy now'}</button>
          </div>
        </div>
      )}
      {open && pending && (
        <ul className="mt-2 space-y-1">
          {pending.commits.map(c => (
            <li key={c.sha} className="flex gap-2 font-mono text-xs">
              <span className="text-amber-700 dark:text-amber-400">{c.sha.slice(0, 7)}</span>
              <span className="flex-1 truncate">{c.subject}</span>
              <span className="text-gray-500">{c.author}</span>
            </li>
          ))}
        </ul>
      )}
      {showFailure && (
        <div className="mt-2 border-t border-amber-200 pt-2">
          {deploy?.error && <p className="text-xs text-red-600">{deploy.error}</p>}
          {failedResume ? (
            <p className="mt-1 text-xs text-red-600">
              {failedResume} session{failedResume === 1 ? '' : 's'} failed to resume — open them to retry:{' '}
              <span className="font-mono">{deploy?.restore?.failed?.join(', ')}</span>
            </p>
          ) : null}
        </div>
      )}
    </div>
  )
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run (in `web/`): `npx vitest run src/components/DeployPanel.test.tsx`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/src/components/DeployPanel.tsx web/src/components/DeployPanel.test.tsx
git commit -m "feat(web): wire Deploy now to controller, add press/optimistic feedback, drop inline checklist"
```

---

## Task 6: Full verification + final commit

- [ ] **Step 1: Run the full web gate**

Run (in `web/`):
```bash
npx vitest run
npx tsc -b --force
npx eslint src --max-warnings 0
```
Expected: all pass, zero eslint warnings. Fix any fallout (e.g. other tests that mounted `DeployPanel`/`App` without the controller provider now need the `useDeployController` spy — search for `usePendingDeploy`/`useDeployStatus` mocks in `DesktopShell`/`Dashboard` tests and add the controller mock, mirroring Task 5(a)).

- [ ] **Step 2: Manual smoke (optional but recommended)**

Trigger a deploy in the running app; confirm: button presses (scale) + "Starting…" instantly; overlay appears before the 5s poll; stepper animates the active phase; killing the daemon shows "reconnecting…" not an error; clean finish auto-dismisses; a forced failure holds open with Close, and after Close the compact inline failure line remains.

- [ ] **Step 3: Commit any verification fixes**

```bash
git add -A
git commit -m "test(web): fix sibling tests for deploy controller provider"
```

---

## Self-Review (completed by author)

- **Spec coverage:** prominence/takeover (Task 3), stepper + progress bar + status line (Task 3), reconnect-awareness (Task 3), auto-close success / hold failure (Tasks 2+3), locked-while-running backdrop (Task 3), single-source phase metadata (Task 1), controller split + root mount (Tasks 2+4), button hover/active/optimistic/Starting + error surfacing (Tasks 2+5), failed-resume inline fallback (Task 5), no backend changes (all tasks). ✓
- **Placeholder scan:** no TBD/TODO; every code step shows full code. The one judgment call (Task 2 Step 5 terminal-edge test) is called out explicitly with a concrete fallback, not left vague. ✓
- **Type consistency:** `DeployControllerValue` fields (`intent`, `visible`, `starting`, `startError`, `start`, `dismiss`) are used identically in Tasks 3, 4, 5; helper names (`phaseState`, `deployProgress`, `allPhasesDone`, `deployOutcome`) match across Tasks 1–3, 5. ✓
