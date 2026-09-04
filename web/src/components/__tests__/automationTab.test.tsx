/**
 * The Automation SECTION of Settings.
 *
 * Task 18: this file used to mount `Dashboard` twice — once with
 * `activeTab="automations"` and once with `activeTab="settings"` — and assert on
 * `role="tabpanel"` ids. There are no tabpanels any more: `/automations` and
 * `/settings/:section` are routes, and the scroll wrapper each page needs lives
 * in `shell/routes.tsx`. That half moved to `routes.test.tsx`
 * ("gives the scrolling pages their own vertical scroll container"), which
 * asserts it against the REAL wrappers; what stays here is the part that was
 * always about `SettingsView` — that Automation is one of its sections and that
 * `?section=automation` selects it.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { SettingsView } from '../settings/SettingsView'

vi.mock('@/api/hooks', () => ({
  // "Reload session" (MainPaneSession → SessionHeader → SessionActions).
  useReloadSession: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
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
  useAutomations: vi.fn(() => ({ data: [], isLoading: false })),
  usePendingDeploy: () => ({ data: { behind: false, commits: [], ci: 'green' } }),
  useDeployStatus: () => ({ data: { running: false, phases: {} } }),
  useStartDeploy: () => ({ mutate: vi.fn(), isPending: false }),
  useAllPrs: vi.fn(() => ({ data: [] })),
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

// Stubbed so the "which section is on screen" assertion has something to name.
// `AutomationView` renders an empty list for `useAutomations() === []`, which is
// indistinguishable from any other empty section — the trap the old tabpanel-id
// assertions fell into.
vi.mock('../AutomationView', () => ({
  AutomationView: () => <div data-testid="section-automation" />,
}))

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

describe('Automation section (Settings)', () => {
  function renderAutomation() {
    return render(
      <MemoryRouter initialEntries={['/settings?section=automation']}>
        <SettingsView />
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

  it('renders the automation view for that section, not the general one', () => {
    // The lower bound the two assertions above do not have: a section strip that
    // highlights "Automation" while rendering General would satisfy both.
    renderAutomation()
    expect(screen.getByTestId('section-automation')).toBeInTheDocument()
  })
})
