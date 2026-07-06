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
  useUpdateAccountNickname: vi.fn(() => ({ mutate: vi.fn(), isPending: false, isSuccess: false })),
  useDeleteAccount: vi.fn(() => ({ mutate: vi.fn(), isPending: false, isSuccess: false })),
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
  usePendingDeploy: () => ({ data: { behind: false, commits: [], ci: 'green' } }),
  useDeployStatus: () => ({ data: { running: false, phases: {} } }),
  useStartDeploy: () => ({ mutate: vi.fn(), isPending: false }),
  useAllPrs: vi.fn(() => ({ data: [] })),
  useStageModels: vi.fn(() => ({ data: {} })),
  useSessionsViewing: vi.fn(),
  useAutomations: vi.fn(() => ({ data: [], isLoading: false })),
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
}))

vi.mock('@/session/useSessionSearch', () => ({
  useSessionSearch: () => ({ query: '', setQuery: vi.fn(), matchedIds: null, isFiltered: false }),
}))

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

describe('Automation tab', () => {
  it('shows the Automation tab button in the tab list', () => {
    render(
      <MemoryRouter>
        <Dashboard activeTab="automation" onTabChange={vi.fn()} onOpenSession={vi.fn()} />
      </MemoryRouter>,
      { wrapper },
    )
    expect(screen.getByRole('tab', { name: 'Automation' })).toBeInTheDocument()
  })

  it('marks the Automation tab as selected when activeTab="automation"', () => {
    render(
      <MemoryRouter>
        <Dashboard activeTab="automation" onTabChange={vi.fn()} onOpenSession={vi.fn()} />
      </MemoryRouter>,
      { wrapper },
    )
    expect(screen.getByRole('tab', { name: 'Automation' })).toHaveAttribute('aria-selected', 'true')
  })

  it('renders the automation tabpanel when the Automation tab is active', () => {
    render(
      <MemoryRouter>
        <Dashboard activeTab="automation" onTabChange={vi.fn()} onOpenSession={vi.fn()} />
      </MemoryRouter>,
      { wrapper },
    )
    const panel = screen.getByRole('tabpanel', { hidden: false })
    expect(panel).toBeTruthy()
    expect(panel.id).toBe('tabpanel-automation')
  })

  it('renders the automation panel inside a vertical scroll container (mobile scroll fix)', () => {
    // In the mobile Dashboard, the tab area is a fixed-height flex column
    // (flex-1 min-h-0). Every tab panel must be its own overflow-y-auto region
    // or its content — e.g. an expanded automation card — overflows the clipped
    // parent with nowhere to scroll. The automation panel used to be mounted
    // bare, unlike its accounts/archived/pipeline/insights siblings.
    render(
      <MemoryRouter>
        <Dashboard activeTab="automation" onTabChange={vi.fn()} onOpenSession={vi.fn()} />
      </MemoryRouter>,
      { wrapper },
    )
    const panel = screen.getByRole('tabpanel', { hidden: false })
    const scroller = panel.closest('.overflow-y-auto')
    expect(scroller).not.toBeNull()
    expect(scroller!.className).toContain('min-h-0')
  })
})
