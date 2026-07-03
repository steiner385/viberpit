// web/src/components/DeployPanel.test.tsx
import { render, screen, fireEvent } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { DeployPanel } from './DeployPanel'
import * as hooks from '@/api/hooks'
import * as controller from '@/lib/deployController'
import type { PendingDeploy, DeployProgress } from '@/api/types'

function mockHooks(
  pending: Partial<PendingDeploy>,
  deploy: Partial<DeployProgress>,
  mutate = vi.fn(),
) {
  vi.spyOn(hooks, 'usePendingDeploy').mockReturnValue(
    { data: pending } as unknown as ReturnType<typeof hooks.usePendingDeploy>)
  vi.spyOn(hooks, 'useDeployStatus').mockReturnValue(
    { data: deploy } as unknown as ReturnType<typeof hooks.useDeployStatus>)
  vi.spyOn(hooks, 'useStartDeploy').mockReturnValue(
    { mutate, isPending: false } as unknown as ReturnType<typeof hooks.useStartDeploy>)
  vi.spyOn(controller, 'useDeployController').mockReturnValue({
    intent: 'idle', visible: false, starting: false, startError: null,
    start: mutate, dismiss: vi.fn(),
  })
}

describe('DeployPanel', () => {
  beforeEach(() => vi.restoreAllMocks())

  it('renders nothing when up to date and idle', () => {
    mockHooks({ behind: false, commits: [], ci: 'green' }, { running: false, phases: {} })
    const { container } = render(<DeployPanel />)
    expect(container).toBeEmptyDOMElement()
  })

  it('shows pending count and enables Deploy when CI green', () => {
    const mutate = vi.fn()
    mockHooks(
      { behind: true, commits: [{ sha: 'abc1234', subject: 'feat x', author: 't', iso: 'i' }], ci: 'green' },
      { running: false, phases: {} }, mutate)
    render(<DeployPanel />)
    expect(screen.getByText(/1 change.*pending/i)).toBeInTheDocument()
    const btn = screen.getByRole('button', { name: /deploy now/i })
    expect(btn).toBeEnabled()
    fireEvent.click(btn)
    expect(mutate).toHaveBeenCalled()   // mutate is wired as controller.start in mockHooks
  })

  it('disables Deploy with reason when CI not green', () => {
    mockHooks({ behind: true, commits: [{ sha: 'a', subject: 's', author: 't', iso: 'i' }], ci: 'pending' },
              { running: false, phases: {} })
    render(<DeployPanel />)
    expect(screen.getByRole('button', { name: /deploy now/i })).toBeDisabled()
    expect(screen.getByText(/CI pending/i)).toBeInTheDocument()
  })

  it('does not render the inline phase checklist (overlay owns it)', () => {
    mockHooks({ behind: true, commits: [], ci: 'green' },
              { running: true, phases: { save: 'done', update: 'running' }, warnings: [] })
    render(<DeployPanel />)
    expect(screen.queryByText('update')).not.toBeInTheDocument()
  })

  it('flags sessions that failed to resume', () => {
    mockHooks({ behind: false, commits: [], ci: 'green' },
              { running: false, phases: { resume: 'done' }, warnings: [],
                restore: { restored: ['s1'], failed: ['s2', 's3'] } })
    render(<DeployPanel />)
    expect(screen.getByText(/2 session.*failed to resume/i)).toBeInTheDocument()
  })
  it('renders nothing after a clean successful deploy (all phases done, no error/failures)', () => {
    mockHooks(
      { behind: false, commits: [], ci: 'green' },
      { running: false,
        phases: { preflight: 'done', save: 'done', update: 'done', report: 'done', restart: 'done', resume: 'done' },
        warnings: ['drain cap — restarting anyway'],
        restore: { restored: ['s1', 's2'], failed: [] } })
    const { container } = render(<DeployPanel />)
    expect(container).toBeEmptyDOMElement()
  })

  it('shows a compact error for a past failed deploy', () => {
    mockHooks({ behind: true, commits: [], ci: 'red' },
              { running: false, phases: { preflight: 'failed' }, warnings: [], error: 'CI verdict=red' })
    render(<DeployPanel />)
    expect(screen.getByText(/CI verdict=red/i)).toBeInTheDocument()
  })

  it('hides a past deploy error once nothing is pending (behind=false)', () => {
    // A health-gate failure rolls back to the previous version, so the app is genuinely
    // on old code — but with no commits left to deploy the error is pure history. Hide it.
    mockHooks({ behind: false, commits: [], ci: 'green' },
              { running: false, phases: { restart: 'failed' }, warnings: [], error: 'restart: health gate failed' })
    const { container } = render(<DeployPanel />)
    expect(screen.queryByText(/health gate failed/i)).not.toBeInTheDocument()
    expect(container).toBeEmptyDOMElement()
  })

  it('keeps showing a past deploy error while commits still await deploy', () => {
    mockHooks({ behind: true, commits: [{ sha: 'a', subject: 's', author: 't', iso: 'i' }], ci: 'green' },
              { running: false, phases: { restart: 'failed' }, warnings: [], error: 'restart: health gate failed' })
    render(<DeployPanel />)
    expect(screen.getByText(/health gate failed/i)).toBeInTheDocument()
  })

  it('still flags failed session resumes even when nothing is pending', () => {
    // Session-resume failures are independent of pending commits (a successful daemon
    // deploy leaves behind=false yet may have lost sessions) — they must survive the hide.
    mockHooks({ behind: false, commits: [], ci: 'green' },
              { running: false, phases: { resume: 'done' }, warnings: [],
                restore: { restored: ['s1'], failed: ['s2'] }, error: null })
    render(<DeployPanel />)
    expect(screen.getByText(/1 session.*failed to resume/i)).toBeInTheDocument()
  })

  it('auto-dismisses a stale preflight failure once live CI goes green', () => {
    // The last deploy failed at the CI gate (verdict was pending/red at the time);
    // the live poller now reports green, so the frozen failure is superseded.
    mockHooks(
      { behind: true, commits: [{ sha: 'a', subject: 's', author: 't', iso: 'i' }], ci: 'green' },
      { running: false, phases: { preflight: 'failed' }, warnings: [], error: 'CI verdict=pending' })
    render(<DeployPanel />)
    // The frozen failure snapshot is gone…
    expect(screen.queryByText(/CI verdict=pending/i)).not.toBeInTheDocument()
    expect(screen.queryByText('failed')).not.toBeInTheDocument()
    // …and the banner falls back to a clean, actionable pending row.
    expect(screen.getByText(/1 change.*pending/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /deploy now/i })).toBeEnabled()
  })

})
