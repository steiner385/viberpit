// web/src/components/DeployPanel.test.tsx
import { render, screen, fireEvent } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { DeployPanel } from './DeployPanel'
import * as hooks from '@/api/hooks'
import * as controller from '@/lib/deployController'
import { DEPLOY_PHASES } from '@/lib/deployPhases'
import type { PendingDeploy, DeployProgress } from '@/api/types'

// Built from DEPLOY_PHASES (not hand-enumerated) so an added/removed phase can't
// silently leave this fixture "all done" one phase short of the real thing.
const allPhasesDoneFixture = Object.fromEntries(DEPLOY_PHASES.map(p => [p.key, 'done']))

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

  it('offers a partial deploy of the green frontier when the tip is red', () => {
    // 5 commits pending, tip CI red, but the 3 oldest are covered by a green commit:
    // show the honest count and deploy THAT sha, not the tip.
    const mutate = vi.fn()
    const commits = ['c5', 'c4', 'c3', 'c2', 'c1'].map(sha => (
      { sha, subject: `s ${sha}`, author: 't', iso: 'i' }))
    mockHooks(
      { behind: true, commits, ci: 'green', tipCi: 'red', deployableSha: 'c3', deployableCount: 3 },
      { running: false, phases: {} }, mutate)
    render(<DeployPanel />)
    expect(screen.getByText(/3 of 5 deployable \(tip CI red\)/i)).toBeInTheDocument()
    const btn = screen.getByRole('button', { name: /deploy now/i })
    expect(btn).toBeEnabled()
    fireEvent.click(btn)
    expect(mutate).toHaveBeenCalledWith('c3')
  })

  it('keeps the red banner when nothing pending is deployable', () => {
    const mutate = vi.fn()
    mockHooks(
      { behind: true, commits: [{ sha: 'c1', subject: 's', author: 't', iso: 'i' }],
        ci: 'red', tipCi: 'red', deployableSha: '', deployableCount: 0 },
      { running: false, phases: {} }, mutate)
    render(<DeployPanel />)
    expect(screen.getByRole('button', { name: /deploy now/i })).toBeDisabled()
    expect(screen.getByText(/CI red/i)).toBeInTheDocument()
    expect(screen.queryByText(/deployable/i)).not.toBeInTheDocument()
  })

  it('deploys the tip (no explicit target) when tip CI is green', () => {
    const mutate = vi.fn()
    mockHooks(
      { behind: true, commits: [{ sha: 'c1', subject: 's', author: 't', iso: 'i' }],
        ci: 'green', tipCi: 'green', deployableSha: 'c1', deployableCount: 1 },
      { running: false, phases: {} }, mutate)
    render(<DeployPanel />)
    fireEvent.click(screen.getByRole('button', { name: /deploy now/i }))
    expect(mutate).toHaveBeenCalledWith(undefined)
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

  it('flags sessions that failed to resume as auto-recovering', () => {
    mockHooks({ behind: false, commits: [], ci: 'green' },
              { running: false, phases: { resume: 'done' }, warnings: [],
                restore: { restored: ['s1'], failed: ['s2', 's3'] } })
    render(<DeployPanel />)
    expect(screen.getByText(/2 session.*failed to resume/i)).toBeInTheDocument()
    expect(screen.getByText(/autopilot is\s+recovering/i)).toBeInTheDocument()
  })

  it('clears the banner once autopilot has recovered every failed session', () => {
    // The daemon-reconciled live set is now empty (all sessions came back) — with
    // nothing pending and no error, the banner must disappear entirely.
    mockHooks({ behind: false, commits: [], ci: 'green' },
              { running: false, phases: { resume: 'done' }, warnings: [],
                restore: { restored: ['s2', 's3'], failed: [], gaveUp: [] } })
    const { container } = render(<DeployPanel />)
    expect(container).toBeEmptyDOMElement()
  })

  it('surfaces gave-up sessions distinctly for a human to retry or archive', () => {
    mockHooks({ behind: false, commits: [], ci: 'green' },
              { running: false, phases: { resume: 'done' }, warnings: [],
                restore: { restored: [], failed: [], gaveUp: ['s9'] } })
    render(<DeployPanel />)
    expect(screen.getByText(/couldn.t be recovered/i)).toBeInTheDocument()
    expect(screen.getByText('s9')).toBeInTheDocument()
  })
  it('renders nothing after a clean successful deploy (all phases done, no error/failures)', () => {
    mockHooks(
      { behind: false, commits: [], ci: 'green' },
      { running: false,
        phases: allPhasesDoneFixture,
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

  describe('dismissing the failure notice', () => {
    const gaveUpDeploy: Partial<DeployProgress> = {
      running: false, phases: { resume: 'done' }, warnings: [],
      restore: { restored: [], failed: [], gaveUp: ['s9'] },
    }

    it('offers a Dismiss control only while a failure is showing', () => {
      mockHooks({ behind: false, commits: [], ci: 'green' }, gaveUpDeploy)
      render(<DeployPanel />)
      expect(screen.getByRole('button', { name: /dismiss/i })).toBeInTheDocument()
    })

    it('does not offer Dismiss on a plain pending-deploy banner', () => {
      // Pending commits are actionable (Deploy button) and clear on their own once
      // deployed — hiding them would hide the only way to deploy.
      mockHooks({ behind: true, commits: [{ sha: 'a', subject: 's', author: 't', iso: 'i' }], ci: 'green' },
                { running: false, phases: {} })
      render(<DeployPanel />)
      expect(screen.queryByRole('button', { name: /dismiss/i })).not.toBeInTheDocument()
    })

    it('hides a gave-up notice that never clears on its own, and stays hidden across remounts', () => {
      mockHooks({ behind: false, commits: [], ci: 'green' }, gaveUpDeploy)
      const first = render(<DeployPanel />)
      fireEvent.click(screen.getByRole('button', { name: /dismiss/i }))
      expect(first.container).toBeEmptyDOMElement()
      first.unmount()
      // A reload re-reads the same live set from the daemon — the dismissal must survive it.
      const second = render(<DeployPanel />)
      expect(second.container).toBeEmptyDOMElement()
    })

    it('comes back when a different failure appears after a dismissal', () => {
      mockHooks({ behind: false, commits: [], ci: 'green' }, gaveUpDeploy)
      const first = render(<DeployPanel />)
      fireEvent.click(screen.getByRole('button', { name: /dismiss/i }))
      first.unmount()
      mockHooks({ behind: false, commits: [], ci: 'green' },
                { ...gaveUpDeploy, restore: { restored: [], failed: [], gaveUp: ['s9', 's10'] } })
      render(<DeployPanel />)
      expect(screen.getByText(/2 sessions couldn.t be recovered/i)).toBeInTheDocument()
    })

    it('keeps the pending-deploy row when only the failure part is dismissed', () => {
      mockHooks({ behind: true, commits: [{ sha: 'a', subject: 's', author: 't', iso: 'i' }], ci: 'green' },
                gaveUpDeploy)
      render(<DeployPanel />)
      fireEvent.click(screen.getByRole('button', { name: /dismiss/i }))
      expect(screen.queryByText(/couldn.t be recovered/i)).not.toBeInTheDocument()
      expect(screen.getByText(/1 change.*pending/i)).toBeInTheDocument()
      expect(screen.getByRole('button', { name: /deploy now/i })).toBeEnabled()
    })
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
