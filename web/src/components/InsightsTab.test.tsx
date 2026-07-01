import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { UseQueryResult, UseMutationResult } from '@tanstack/react-query'
import * as hooks from '../api/hooks'
import { Dashboard } from './Dashboard'

function stub<T>(d: T): UseQueryResult<T> {
  return { data: d, dataUpdatedAt: 0, isLoading: false, isError: false } as unknown as UseQueryResult<T>
}

function mutStub(): UseMutationResult<unknown, unknown, unknown, unknown> {
  return { mutate: vi.fn(), isPending: false } as unknown as UseMutationResult<unknown, unknown, unknown, unknown>
}

function mockDashboardHooks() {
  vi.spyOn(hooks, 'useAccounts').mockReturnValue(stub([]))
  vi.spyOn(hooks, 'useSessions').mockReturnValue(stub([]))
  vi.spyOn(hooks, 'useSessionsViewing').mockReturnValue(undefined)
  vi.spyOn(hooks, 'useArchivedSessions').mockReturnValue(stub([]))
  vi.spyOn(hooks, 'useSettings').mockReturnValue(stub(null as never))
  vi.spyOn(hooks, 'useConfig').mockReturnValue(stub(null as never))
  vi.spyOn(hooks, 'useGithubRepos').mockReturnValue(stub([]))
  vi.spyOn(hooks, 'useCreateSession').mockReturnValue(mutStub() as never)
  vi.spyOn(hooks, 'useRenameSession').mockReturnValue(mutStub() as never)
  vi.spyOn(hooks, 'useCloseSession').mockReturnValue(mutStub() as never)
  vi.spyOn(hooks, 'usePermanentDeleteSession').mockReturnValue(mutStub() as never)
  vi.spyOn(hooks, 'useSendPrompt').mockReturnValue(mutStub() as never)
  vi.spyOn(hooks, 'useStageModels').mockReturnValue(stub({ 'o/n': {} as never }))
  vi.spyOn(hooks, 'useCheckInsights').mockReturnValue(stub({
    repo: 'o/n', window: '7d', slowest: [], flakiest: [],
  }))
}

describe('Insights tab', () => {
  beforeEach(() => vi.restoreAllMocks())

  it('renders InsightsView when the active tab is insights', () => {
    mockDashboardHooks()
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <MemoryRouter>
        <QueryClientProvider client={qc}>
          <Dashboard activeTab="insights" />
        </QueryClientProvider>
      </MemoryRouter>,
    )
    expect(screen.getByText(/Slowest \/ most-variable/i)).toBeInTheDocument()
  })
})
