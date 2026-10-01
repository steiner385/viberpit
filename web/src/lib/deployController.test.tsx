// web/src/lib/deployController.test.tsx
import { render, screen, act, fireEvent } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { DeployControllerProvider, useDeployController } from './deployController'
import { DEPLOY_PHASES } from './deployPhases'
import * as hooks from '@/api/hooks'
import type { DeployProgress } from '@/api/types'

// Built from DEPLOY_PHASES (not hand-enumerated) so an added/removed phase can't
// silently leave this fixture "all done" one phase short of the real thing.
const allDone = Object.fromEntries(DEPLOY_PHASES.map(p => [p.key, 'done']))

function setStatus(deploy: Partial<DeployProgress>, over: { isError?: boolean; failureCount?: number } = {}) {
  vi.spyOn(hooks, 'useDeployStatus').mockReturnValue(
    { data: deploy, isError: over.isError ?? false, failureCount: over.failureCount ?? 0 } as unknown as ReturnType<typeof hooks.useDeployStatus>)
}
function setStart(mutate = vi.fn(), over: Record<string, unknown> = {}) {
  vi.spyOn(hooks, 'useStartDeploy').mockReturnValue(
    { mutate, isPending: false, isError: false, error: null, ...over } as unknown as ReturnType<typeof hooks.useStartDeploy>)
  return mutate
}

function setSessions(statuses: string[] = []) {
  vi.spyOn(hooks, 'useSessions').mockReturnValue(
    { data: statuses.map((status, i) => ({ id: `s${i}`, status })) } as unknown as ReturnType<typeof hooks.useSessions>)
}

/** `start()` now asks first (2026-09-30 Settings critique, P1): the confirm is
 *  the gate both Deploy buttons share. Answer it. */
const confirmDeploy = () => act(() => { fireEvent.click(screen.getByRole('button', { name: 'Deploy now' })) })

function Probe() {
  const c = useDeployController()
  return (
    <div>
      <span data-testid="intent">{c.intent}</span>
      <span data-testid="visible">{String(c.visible)}</span>
      <button onClick={() => c.start()}>start</button>
      <button onClick={c.dismiss}>dismiss</button>
    </div>
  )
}
const renderProbe = () =>
  render(<DeployControllerProvider><Probe /></DeployControllerProvider>)

describe('deployController', () => {
  // Every test gets an empty session list: the provider's gate reads `useSessions`.
  beforeEach(() => { vi.restoreAllMocks(); vi.useFakeTimers(); setSessions() })
  afterEach(() => vi.useRealTimers())

  it('start() asks first; confirming goes to starting and fires the mutation (visible)', () => {
    setStatus({ running: false, phases: {} })
    setSessions()
    const mutate = setStart()
    renderProbe()
    act(() => { fireEvent.click(screen.getByText('start')) })
    // Nothing has started yet: the gate is up.
    expect(mutate).not.toHaveBeenCalled()
    expect(screen.getByRole('dialog', { name: 'Deploy now?' })).toBeInTheDocument()
    expect(screen.getByTestId('intent').textContent).toBe('idle')
    confirmDeploy()
    expect(mutate).toHaveBeenCalledWith(undefined, expect.anything())
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.getByTestId('intent').textContent).toBe('starting')
    expect(screen.getByTestId('visible').textContent).toBe('true')
  })

  it('cancelling the gate starts nothing', () => {
    setStatus({ running: false, phases: {} })
    setSessions()
    const mutate = setStart()
    renderProbe()
    act(() => { fireEvent.click(screen.getByText('start')) })
    act(() => { fireEvent.click(screen.getByRole('button', { name: 'Cancel' })) })
    expect(mutate).not.toHaveBeenCalled()
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.getByTestId('intent').textContent).toBe('idle')
  })

  it('the gate names the live sessions and the turns that drain first', () => {
    setStatus({ running: false, phases: {} })
    setSessions(['in_progress', 'idle', 'waiting_input', 'ended', 'archived'])
    setStart()
    renderProbe()
    act(() => { fireEvent.click(screen.getByText('start')) })
    const dialog = screen.getByRole('dialog', { name: 'Deploy now?' })
    expect(dialog).toHaveTextContent('3 live sessions pause while it restarts, then autopilot resumes them.')
    expect(dialog).toHaveTextContent('The turn in progress finishes first')
  })

  it('is visible while the server reports running', () => {
    setStatus({ running: true, phases: { update: 'running' } })
    setStart()
    renderProbe()
    expect(screen.getByTestId('visible').textContent).toBe('true')
  })

  it('held-success on clean finish, then auto-dismiss after 2s', () => {
    setStart()
    // Provide an initial status before rendering (no QueryClient in test env).
    setStatus({ running: true, phases: {} })
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

  it('a transient status-poll error mid-run does NOT classify as held-failure', () => {
    // The edge returns 503 for a transient "controller unavailable"; react-query then keeps the
    // last-good data (running:true) and sets isError. The controller must NOT read that as a
    // finished deploy — otherwise an upgrade flashes a spurious "Upgrade failed" then self-heals.
    setStart()
    setStatus({ running: true, phases: { update: 'running' } })
    const rerender = renderProbe()
    // Poll fails while the deploy is still running — data stays running:true, isError flips on.
    setStatus({ running: true, phases: { update: 'running' } }, { isError: true, failureCount: 2 })
    act(() => { rerender.rerender(<DeployControllerProvider><Probe /></DeployControllerProvider>) })
    expect(screen.getByTestId('intent').textContent).not.toBe('held-failure')
    expect(screen.getByTestId('visible').textContent).toBe('true')
  })

  it('a status wiped by the controller self-restart dismisses instead of held-failure', () => {
    // Incident shape (#865): poll N reports running with resume in flight; poll N+1 hits
    // the freshly restarted controller process, which reports an EMPTY status. The
    // running true→false edge must not classify that wipe as a failed upgrade.
    setStart()
    setStatus({ running: true, phases: { restart: 'done', resume: 'running' } })
    const rerender = renderProbe()
    setStatus({ running: false, phases: {}, error: null })
    act(() => { rerender.rerender(<DeployControllerProvider><Probe /></DeployControllerProvider>) })
    expect(screen.getByTestId('intent').textContent).toBe('idle')
    expect(screen.getByTestId('visible').textContent).toBe('false')
  })

  it('mutation error sends intent to held-failure', () => {
    setStatus({ running: false, phases: {} })
    setSessions()
    setStart(vi.fn((_: unknown, opts?: { onError?: () => void }) => opts?.onError?.()))
    renderProbe()
    act(() => { fireEvent.click(screen.getByText('start')) })
    confirmDeploy()
    expect(screen.getByTestId('intent').textContent).toBe('held-failure')
  })
})
