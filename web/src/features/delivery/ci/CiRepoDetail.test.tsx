import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { CiRepoDetail } from './CiRepoDetail'
import type { Automation } from '@/api/automations'
import type { CheckInsights } from '@/api/types'

const DATA: CheckInsights = {
  repo: 'o/r',
  window: '7d',
  slowest: [{ check_name: 'e2e', event: 'pull_request', p50: 125, p90: 500, ratio: 1.2, n: 9 }],
  flakiest: [{ check_name: 'ws', event: 'pull_request', flake_pct: 5, flaky_commits: 1, total_commits: 20, n: 20 }],
  regressions: [{ check_name: 'build', event: 'push', prior_p50: 200, recent_p50: 400, ratio: 2.0, since: '2026-01-01T00:00:00Z' }],
  runner_wait_health: [{ event: 'push', p50: 20, p90: 30, prev_p50: 10, n: 5 }],
}

// The CI/CD Doctor's row, the single source of the flake thresholds.
const DOCTOR = {
  id: 'cicd-doctor', name: 'CI/CD Doctor', description: '', enabled: true, mode: 'file', modes: ['off', 'file'],
  status: { text: 'Idle', level: 'idle', counts: null }, actionableCount: 0,
  configSchema: [
    { key: 'max_flake_pct', label: '', type: 'int', default: 2, help: '', choices: null },
    { key: 'demote_flake_pct', label: '', type: 'int', default: 5, help: '', choices: null },
  ],
  config: {}, lastActiveAt: null, activeSessions: 0,
} as unknown as Automation

const useCheckInsights = vi.fn()
vi.mock('@/api/hooks', () => ({
  useCheckInsights: (...args: unknown[]) => useCheckInsights(...args),
}))

const mount = (props: Partial<React.ComponentProps<typeof CiRepoDetail>> = {}) =>
  render(<MemoryRouter><CiRepoDetail repo="o/r" window="7d" {...props} /></MemoryRouter>)

describe('CiRepoDetail', () => {
  it('renders all four check tables with formatted cells', () => {
    useCheckInsights.mockReturnValue({ data: DATA, isLoading: false, isError: false })
    mount()

    expect(screen.getByText('Slowest / most-variable checks')).toBeInTheDocument()
    expect(screen.getByText('Flakiest checks')).toBeInTheDocument()
    expect(screen.getByText('Duration regressions (live — not affected by window)')).toBeInTheDocument()
    expect(screen.getByText('Runner-wait health')).toBeInTheDocument()

    // fmtSecs formatting: 400s -> "6m 40s"
    expect(screen.getByText('6m 40s')).toBeInTheDocument()
    // regression Slowdown cell
    expect(screen.getByText('2.0×')).toBeInTheDocument()
    // runner-wait delta: p50=20, prev_p50=10 -> up 10s
    expect(screen.getByText('↑ 10s')).toBeInTheDocument()
  })

  // 2026-10-01 Delivery critique, P2: 12px numbers with no tone or threshold,
  // no lane vocabulary, no link to the automation that acts on them.
  it('leads with a verdict in the lane vocabulary, judged by the CI/CD Doctor\'s thresholds, and links the doctor', () => {
    useCheckInsights.mockReturnValue({ data: DATA, isLoading: false, isError: false })
    mount({ automations: [DOCTOR] })
    const band = screen.getByRole('group', { name: 'CI health verdict' })
    // ws flakes at 5% = the demotion rate → red; the regression rides along.
    expect(band).toHaveTextContent(/red/)
    expect(band).toHaveTextContent('1 flaky ≥5% · 1 regression')
    expect(band).toHaveTextContent('watch ≥2% · red ≥5% flake')
    expect(screen.getByRole('link', { name: 'Open CI/CD Doctor' })).toHaveAttribute('href', '/delivery/automations/cicd-doctor')
    // The number carries the tone beside it.
    expect(screen.getByText('5.0%').className).toMatch(/\btext-danger\b/)
    expect(screen.getByText('2.0×').className).toMatch(/\btext-warning\b/)
    expect(screen.getByText('↑ 10s').className).toMatch(/\btext-warning\b/)
  })

  it('without the doctor\'s row, flake rates are shown untoned and the verdict rests on what is still judged', () => {
    useCheckInsights.mockReturnValue({ data: DATA, isLoading: false, isError: false })
    mount()
    const band = screen.getByRole('group', { name: 'CI health verdict' })
    expect(band).toHaveTextContent(/watch/)
    expect(band).toHaveTextContent('1 regression')
    expect(band).not.toHaveTextContent(/flaky/)
    expect(screen.getByText('5.0%').className).not.toMatch(/text-danger/)
  })

  it('says idle, not green, when the window has no check history', () => {
    useCheckInsights.mockReturnValue({ data: { ...DATA, slowest: [], flakiest: [], regressions: [] }, isLoading: false, isError: false })
    mount({ automations: [DOCTOR] })
    const band = screen.getByRole('group', { name: 'CI health verdict' })
    expect(band).toHaveTextContent(/idle/)
    expect(band).toHaveTextContent('No check history in this window')
  })

  it('shows a danger banner on error', () => {
    useCheckInsights.mockReturnValue({ data: undefined, isLoading: false, isError: true })
    mount()
    expect(screen.getByRole('alert')).toHaveTextContent('Could not load CI health.')
  })

  it('shows skeletons while loading with no data yet', () => {
    useCheckInsights.mockReturnValue({ data: undefined, isLoading: true, isError: false })
    const { container } = mount()
    expect(container.querySelectorAll('.animate-pulse').length).toBeGreaterThan(0)
  })
})
