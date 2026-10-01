// web/src/shell/DeployPanel.test.tsx
//
// The banner is FAILURE-ONLY (2026-10-01 shell critique, P2). Pending commits —
// the count, the CI verdict, the green-frontier target, Deploy now — are the
// `DeployChip`'s to point at and Settings › Deploy's to show; their behaviour is
// pinned in `DeploySection.test.tsx`, not here.
import { render, screen, fireEvent } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { DeployPanel } from './DeployPanel'
import * as hooks from '@/api/hooks'
import { DEPLOY_PHASES } from '@/lib/deployPhases'
import type { PendingDeploy, DeployProgress } from '@/api/types'

// Built from DEPLOY_PHASES (not hand-enumerated) so an added/removed phase can't
// silently leave this fixture "all done" one phase short of the real thing.
const allPhasesDoneFixture = Object.fromEntries(DEPLOY_PHASES.map(p => [p.key, 'done']))

function mockHooks(pending: Partial<PendingDeploy>, deploy: Partial<DeployProgress>) {
  vi.spyOn(hooks, 'usePendingDeploy').mockReturnValue(
    { data: pending } as unknown as ReturnType<typeof hooks.usePendingDeploy>)
  vi.spyOn(hooks, 'useDeployStatus').mockReturnValue(
    { data: deploy } as unknown as ReturnType<typeof hooks.useDeployStatus>)
}

const ONE_PENDING: Partial<PendingDeploy> = {
  behind: true, commits: [{ sha: 'abc1234', subject: 'feat x', author: 't', iso: 'i' }], ci: 'green',
}

describe('DeployPanel', () => {
  beforeEach(() => vi.restoreAllMocks())

  it('renders nothing when up to date and idle', () => {
    mockHooks({ behind: false, commits: [], ci: 'green' }, { running: false, phases: {} })
    const { container } = render(<DeployPanel />)
    expect(container).toBeEmptyDOMElement()
  })

  it('renders nothing for plain pending commits: the rail chip and Settings › Deploy own them', () => {
    mockHooks(ONE_PENDING, { running: false, phases: {} })
    const { container } = render(<DeployPanel />)
    expect(container).toBeEmptyDOMElement()
    expect(screen.queryByRole('button', { name: /deploy now/i })).toBeNull()
  })

  it('renders nothing while a deploy runs (the overlay owns the phase checklist)', () => {
    mockHooks({ behind: true, commits: [], ci: 'green' },
              { running: true, phases: { save: 'done', update: 'running' }, warnings: [] })
    const { container } = render(<DeployPanel />)
    expect(container).toBeEmptyDOMElement()
  })

  it('a failure is a warning banner', () => {
    // `ci: 'red'` — a preflight failure is considered stale (and hidden) once CI
    // is green again, exactly as the "compact error" case below is set up.
    mockHooks(
      { ...ONE_PENDING, ci: 'red' },
      { running: false, phases: { preflight: 'failed' }, warnings: [], error: 'CI verdict=red' })
    const { container } = render(<DeployPanel />)
    expect(container.firstElementChild?.className).toMatch(/bg-warning-wash/)
    expect(screen.getByText(/CI verdict=red/i)).toBeInTheDocument()
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
    mockHooks(ONE_PENDING,
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

  it('a stale preflight failure superseded by green live CI shows nothing', () => {
    // The last deploy failed at the CI gate (verdict was pending/red at the time);
    // the live poller now reports green, so the frozen failure is superseded — and
    // the pending commits it leaves behind are the chip's, not the banner's.
    mockHooks(ONE_PENDING,
      { running: false, phases: { preflight: 'failed' }, warnings: [], error: 'CI verdict=pending' })
    const { container } = render(<DeployPanel />)
    expect(screen.queryByText(/CI verdict=pending/i)).not.toBeInTheDocument()
    expect(container).toBeEmptyDOMElement()
  })

  describe('dismissing the failure notice', () => {
    const gaveUpDeploy: Partial<DeployProgress> = {
      running: false, phases: { resume: 'done' }, warnings: [],
      restore: { restored: [], failed: [], gaveUp: ['s9'] },
    }

    it('offers a Dismiss control', () => {
      mockHooks({ behind: false, commits: [], ci: 'green' }, gaveUpDeploy)
      render(<DeployPanel />)
      expect(screen.getByRole('button', { name: /dismiss/i })).toBeInTheDocument()
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
  })
})
