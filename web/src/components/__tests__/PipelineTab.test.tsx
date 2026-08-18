import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Dashboard } from '../Dashboard'

vi.mock('@/api/hooks', () => ({
  useConfig: vi.fn(() => ({ data: { adoptionEnabled: false } })),
  useAdoptable: vi.fn(() => ({ data: { candidates: [] } })),
  useAdoptSession: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useAccounts: vi.fn(() => ({ data: [], isLoading: false })),
  useSessions: vi.fn(() => ({ data: [], isLoading: false })),
  useCreateSession: vi.fn(() => ({ mutate: vi.fn(), isPending: false, isSuccess: false })),
  useRenameSession: vi.fn(() => ({ mutate: vi.fn(), isPending: false, isSuccess: false })),
  useCloseSession: vi.fn(() => ({ mutate: vi.fn(), isPending: false, isSuccess: false })),
  useRefreshAccount: vi.fn(() => ({ mutate: vi.fn(), isPending: false, isSuccess: false })),
  useUpdateAccount: vi.fn(() => ({ mutate: vi.fn(), isPending: false, isSuccess: false })),
  useDeleteAccount: vi.fn(() => ({ mutate: vi.fn(), isPending: false, isSuccess: false })),
  useSetAccountEnabled: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useGithubRepos: vi.fn(() => ({ data: [], isLoading: false, isError: false })),
  useModels: vi.fn(() => ({ data: [] })),
  useGithubBranches: vi.fn(() => ({ data: { default: null, branches: [] }, isLoading: false })),
  useSettings: vi.fn(() => ({ data: { defaultPermissionMode: 'interactive', thinkingCollapsed: false, repoRoot: null }, isLoading: false })),
  useUpdateSettings: vi.fn(() => ({ mutate: vi.fn(), isPending: false, isSuccess: false })),
  useReauthAccount: vi.fn(() => ({ mutate: vi.fn(), isPending: false, isSuccess: false })),
  useReauthStatus: vi.fn(() => ({ data: { state: 'idle', message: null, startedAt: null }, isLoading: false })),
  useCancelReauth: vi.fn(() => ({ mutate: vi.fn(), isPending: false, isSuccess: false })),
  useArchivedSessions: vi.fn(() => ({ data: [], isLoading: false })),
  useUnarchiveSession: vi.fn(() => ({ mutate: vi.fn(), isPending: false, isSuccess: false })),
  usePermanentDeleteSession: vi.fn(() => ({ mutate: vi.fn(), isPending: false, isSuccess: false })),
  useBulkUnarchiveSessions: vi.fn(() => ({ mutate: vi.fn(), isPending: false, isSuccess: false })),
  useBulkPermanentDeleteSessions: vi.fn(() => ({ mutate: vi.fn(), isPending: false, isSuccess: false })),
  useSendPrompt: () => ({ mutate: vi.fn() }),
  useSetAutopilotPaused: () => ({ mutate: vi.fn() }),
  useFixPr: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useAutomations: vi.fn(() => ({ data: [] })),
  usePendingDeploy: () => ({ data: { behind: false, commits: [], ci: 'green' } }),
  useDeployStatus: () => ({ data: { running: false, phases: {} } }),
  useStartDeploy: () => ({ mutate: vi.fn(), isPending: false }),
  useAllPrs: vi.fn(() => ({ data: [], dataUpdatedAt: 0 })),
  useStageModels: vi.fn(() => ({ data: {} })),
  useSessionsViewing: vi.fn(),
  useAddCopilotAccount: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useCopilotAddStatus: vi.fn(() => ({ data: undefined, isLoading: false })),
  useCancelCopilotAdd: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  QK: {
    accounts: ['accounts'],
    sessions: ['sessions'],
    archived: ['archived-sessions'],
    config: ['config'],
    githubRepos: ['github-repos'],
    githubBranches: (repo?: string) => ['github-branches', repo],
    settings: ['settings'],
  },
  useAssessDispatch: vi.fn(() => ({ assess: vi.fn() })),
}))

vi.mock('@/session/useSessionSearch', () => ({
  useSessionSearch: () => ({ query: '', setQuery: vi.fn(), matchedIds: null, isFiltered: false }),
}))

// The pipeline tab now hosts the ported plotroom island (PipelineTab), which
// opens an SSE EventSource on mount — undefined in jsdom. PipelineTab has its own
// suite (src/pipeline/__tests__/PipelineTab.test.tsx); here we only assert that
// Dashboard mounts it for the active pipeline tab, so a stub is sufficient.
vi.mock('@/pipeline/PipelineTab', () => ({
  PipelineTab: () => <div>pipeline-tab-stub</div>,
}))

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

describe('Pipeline tab', () => {
  it('mounts the PipelineTab island when the pipeline tab is active', () => {
    render(
      <MemoryRouter>
        <Dashboard activeTab="pipeline" onTabChange={vi.fn()} onOpenSession={vi.fn()} />
      </MemoryRouter>,
      { wrapper },
    )
    // the pipeline tab now hosts the ported plotroom PipelineTab island
    expect(screen.getByText('pipeline-tab-stub')).toBeInTheDocument()
  })
})
