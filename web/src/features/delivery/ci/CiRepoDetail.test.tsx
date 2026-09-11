import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { CiRepoDetail } from './CiRepoDetail'
import type { CheckInsights } from '@/api/types'

const DATA: CheckInsights = {
  repo: 'o/r',
  window: '7d',
  slowest: [{ check_name: 'e2e', event: 'pull_request', p50: 125, p90: 500, ratio: 1.2, n: 9 }],
  flakiest: [{ check_name: 'ws', event: 'pull_request', flake_pct: 5, flaky_commits: 1, total_commits: 20, n: 20 }],
  regressions: [{ check_name: 'build', event: 'push', prior_p50: 200, recent_p50: 400, ratio: 2.0, since: '2026-01-01T00:00:00Z' }],
  runner_wait_health: [{ event: 'push', p50: 20, p90: 30, prev_p50: 10, n: 5 }],
}

const useCheckInsights = vi.fn()
vi.mock('@/api/hooks', () => ({
  useCheckInsights: (...args: unknown[]) => useCheckInsights(...args),
}))

describe('CiRepoDetail', () => {
  it('renders all four check tables with formatted cells', () => {
    useCheckInsights.mockReturnValue({ data: DATA, isLoading: false, isError: false })
    render(<CiRepoDetail repo="o/r" window="7d" />)

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

  it('shows a danger banner on error', () => {
    useCheckInsights.mockReturnValue({ data: undefined, isLoading: false, isError: true })
    render(<CiRepoDetail repo="o/r" window="7d" />)
    expect(screen.getByRole('alert')).toHaveTextContent('Could not load CI health.')
  })

  it('shows skeletons while loading with no data yet', () => {
    useCheckInsights.mockReturnValue({ data: undefined, isLoading: true, isError: false })
    const { container } = render(<CiRepoDetail repo="o/r" window="7d" />)
    expect(container.querySelectorAll('.animate-pulse').length).toBeGreaterThan(0)
  })
})
