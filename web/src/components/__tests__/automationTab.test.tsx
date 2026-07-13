import { describe, it, expect, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'
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

describe('Automations top-level tab', () => {
  function renderAutomationsTab() {
    return render(
      <MemoryRouter>
        <Dashboard activeTab="automations" onTabChange={vi.fn()} onOpenSession={vi.fn()} />
      </MemoryRouter>,
      { wrapper },
    )
  }

  it('renders the automations tabpanel', () => {
    renderAutomationsTab()
    const panel = screen.getByRole('tabpanel', { hidden: false })
    expect(panel.id).toBe('tabpanel-automations')
  })

  it('automations panel is a scroll container (scroll-wrapper invariant)', () => {
    renderAutomationsTab()
    const panel = screen.getByRole('tabpanel', { hidden: false })
    expect(panel.className).toContain('overflow-y-auto')
    expect(panel.className).toContain('min-h-0')
  })
})

describe('Automation section (Settings tab)', () => {
  // Automation lives inside the Settings tab as a ?section=automation section.
  function renderAutomation() {
    return render(
      <MemoryRouter initialEntries={['/settings?section=automation']}>
        <Dashboard activeTab="settings" onTabChange={vi.fn()} onOpenSession={vi.fn()} />
      </MemoryRouter>,
      { wrapper },
    )
  }

  it('shows the Automation section tab in the Settings section strip', () => {
    renderAutomation()
    const sections = within(screen.getByRole('tablist', { name: /settings sections/i }))
    expect(sections.getByRole('tab', { name: 'Automation' })).toBeInTheDocument()
  })

  it('marks the Automation section as selected when ?section=automation', () => {
    renderAutomation()
    const sections = within(screen.getByRole('tablist', { name: /settings sections/i }))
    expect(sections.getByRole('tab', { name: 'Automation' })).toHaveAttribute('aria-selected', 'true')
  })

  it('renders the settings tabpanel when the Settings tab is active', () => {
    renderAutomation()
    const panel = screen.getByRole('tabpanel', { hidden: false })
    expect(panel).toBeTruthy()
    expect(panel.id).toBe('tabpanel-settings')
  })

  it('renders the automation section inside a vertical scroll container (mobile scroll fix)', () => {
    // In the mobile Dashboard, the tab area is a fixed-height flex column
    // (flex-1 min-h-0). Every tab panel must be its own overflow-y-auto region
    // or its content — e.g. an expanded automation card — overflows the clipped
    // parent with nowhere to scroll. The settings panel owns that scroll region
    // for all of its sections, automation included.
    renderAutomation()
    const panel = screen.getByRole('tabpanel', { hidden: false })
    expect(panel.className).toContain('overflow-y-auto')
    expect(panel.className).toContain('min-h-0')
  })
})
