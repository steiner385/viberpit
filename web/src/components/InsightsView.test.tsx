import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import type { UseQueryResult } from '@tanstack/react-query'
import * as hooks from '../api/hooks'
import { InsightsView } from './InsightsView'

function stub<T>(data: T): UseQueryResult<T> {
  return { data, dataUpdatedAt: 0 } as unknown as UseQueryResult<T>
}

describe('InsightsView', () => {
  beforeEach(() => vi.restoreAllMocks())
  it('renders both panels for the selected repo', () => {
    vi.spyOn(hooks, 'useStageModels').mockReturnValue(stub({ 'o/n': {} as never }))
    vi.spyOn(hooks, 'useCheckInsights').mockReturnValue(stub({
      repo: 'o/n', window: '7d',
      slowest: [{ check_name: 'build', event: 'pr', p50: 120, p90: 300, ratio: 2.5, n: 9 }],
      flakiest: [{ check_name: 'e2e', event: 'pr', flake_pct: 33.3, flaky_commits: 1, total_commits: 3, n: 4 }],
    }))
    render(<InsightsView />)
    expect(screen.getByText(/Slowest/i)).toBeInTheDocument()
    expect(screen.getByText(/Flakiest/i)).toBeInTheDocument()
    expect(screen.getByText('build')).toBeInTheDocument()
    expect(screen.getByText('e2e')).toBeInTheDocument()
  })
})
