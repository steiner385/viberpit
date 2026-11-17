import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import type { MilestoneDelivery } from '@/api/milestones'
import { DeliveryPanel } from './DeliveryPanel'

const BLOCKED: MilestoneDelivery = {
  state: 'blocked',
  repo: 'cairnea/KinDash',
  branch: 'main',
  prodSha: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
  behindCommits: 14,
  hoursBehind: 57,
  oldestUnshippedAt: '2026-11-08T03:00:00+00:00',
  verified: true,
  verifiedBy: 'https://run/1',
  gateWorkflows: ['nightly.yml', 'deploy-reattempt.yml'],
  streak: 0,
  streakUrl: 'https://run/2',
  regressed: [
    { ref: 'cairnea/KinDash#21010', title: 'flaky', url: 'https://github.com/cairnea/KinDash/issues/21010', session: false, prs: [], owned: false },
    { ref: 'cairnea/KinDash#21011', title: 'flaky 2', url: 'https://github.com/cairnea/KinDash/issues/21011', session: true, prs: [], owned: true },
  ],
  escalated: true,
  reasons: ['blocked'],
  blockers: ['cairnea/KinDash#21010', 'cairnea/KinDash#21011'],
  compareUrl: 'https://github.com/cairnea/KinDash/compare/aaaa...main',
  errors: [],
  measuredAt: '2026-11-10T12:00:00+00:00',
  ageSeconds: 60,
  error: null,
}

describe('DeliveryPanel', () => {
  it('renders nothing for a product without a prod deploy environment', () => {
    const { container } = render(<DeliveryPanel delivery={null} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('shows the state, distance from main, streak and who is on each regressed issue', () => {
    render(<DeliveryPanel delivery={BLOCKED} />)
    expect(screen.getByRole('heading', { name: 'Prod delivery' })).toBeInTheDocument()
    expect(screen.getByText('Blocked')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'aaaaaaa' })).toHaveAttribute('href', BLOCKED.compareUrl)
    expect(screen.getByText('14 commits · 57 h behind main')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Verified by a green gate run' })).toHaveAttribute('href', 'https://run/1')
    expect(screen.getByRole('link', { name: '0 green' })).toBeInTheDocument()
    const regressed = screen.getByRole('list', { name: 'Regressed issues' })
    expect(regressed).toHaveTextContent('cairnea/KinDash#21010 · nobody on it')
    expect(regressed).toHaveTextContent('cairnea/KinDash#21011 · session on it')
  })

  it('names the gate workflows when prod runs an unverified SHA', () => {
    render(<DeliveryPanel delivery={{ ...BLOCKED, state: 'unverified', verified: false, verifiedBy: null, behindCommits: 0, hoursBehind: 0 }} />)
    expect(screen.getByText('Unverified')).toBeInTheDocument()
    expect(screen.getByText('Level with main')).toBeInTheDocument()
    expect(screen.getByText('Not verified by nightly.yml or deploy-reattempt.yml')).toBeInTheDocument()
  })

  it('says so when the health endpoint did not answer', () => {
    render(<DeliveryPanel delivery={{ ...BLOCKED, state: 'unknown', prodSha: null, regressed: null }} />)
    expect(screen.getByText('Unknown')).toBeInTheDocument()
    expect(screen.getByText('The prod health endpoint did not answer.')).toBeInTheDocument()
  })
})
