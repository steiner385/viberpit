import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

// ── Library section mocks (mirrors LibraryView.test.tsx) ────────────────────
vi.mock('@/api/client', () => ({
  ApiRequestError: class extends Error {},
  api: {
    get: vi.fn().mockImplementation((path: string) => {
      if (path === '/library/plugins') {
        return Promise.resolve({
          plugins: [{ id: 'alpha@m', name: 'alpha', marketplace: 'm', enabled: true, description: 'Alpha plugin', components: { commands: 1, skills: 0, agents: 0, mcpServers: 0, hooks: 0 } }],
          marketplaces: [],
        })
      }
      if (path === '/library/skills') return Promise.resolve({ skills: [{ name: 'my-skill', source: 'user', readonly: false }] })
      if (path === '/library/commands') return Promise.resolve({ commands: [] })
      if (path === '/library/mcp') return Promise.resolve({ servers: [] })
      return Promise.resolve({})
    }),
    post: vi.fn(), put: vi.fn(), patch: vi.fn(), del: vi.fn(),
  },
}))

// ── API hooks mock — covers GeneralSection, AutomationView, AccountsManager ─
vi.mock('@/api/hooks', () => ({
  // shared
  QK: {},
  // GeneralSection
  useSettings: vi.fn(() => ({ data: { defaultPermissionMode: 'interactive', thinkingCollapsed: false, repoRoot: '/home/user/repos', defaultToolDetail: 'auto' }, isLoading: false })),
  useUpdateSettings: vi.fn(() => ({ mutate: vi.fn(), isPending: false, isSuccess: false })),
  // AutomationView
  useAutomations: vi.fn(() => ({ data: [], isLoading: false })),
  // AccountsManager
  useAccounts: vi.fn(() => ({ data: [], isLoading: false })),
  useSessions: vi.fn(() => ({ data: [], isLoading: false })),
  useRefreshAccount: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useUpdateAccount: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useDeleteAccount: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useSetAccountEnabled: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useReauthAccount: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useReauthStatus: vi.fn(() => ({ data: undefined, isLoading: false })),
  useCancelReauth: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useSubmitReauthCode: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  // AddAccountDialog (rendered inside AccountsManager)
  useAddAccount: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useAddAccountStatus: vi.fn(() => ({ data: undefined, isLoading: false })),
  useCancelAddAccount: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useAddCopilotAccount: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useCopilotAddStatus: vi.fn(() => ({ data: undefined, isLoading: false })),
  useCancelCopilotAdd: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  // Library section hooks
  useLibraryPlugins: vi.fn(() => ({ data: { plugins: [{ id: 'alpha@m', name: 'alpha', marketplace: 'm', enabled: true, description: 'Alpha plugin', components: { commands: 1, skills: 0, agents: 0, mcpServers: 0, hooks: 0 } }], marketplaces: [] }, isLoading: false })),
  useLibraryPluginDetail: vi.fn(() => ({ data: undefined, isLoading: false })),
  useTogglePlugin: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useLibrarySkills: vi.fn(() => ({ data: { skills: [{ name: 'my-skill', source: 'user', readonly: false }] }, isLoading: false })),
  useLibraryCommands: vi.fn(() => ({ data: { commands: [] }, isLoading: false })),
  useLibraryMcp: vi.fn(() => ({ data: { servers: [] }, isLoading: false })),
  useLibraryItemContent: vi.fn(() => ({ data: undefined, isLoading: false })),
  useSaveLibraryItem: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useDeleteLibraryItem: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useSaveMcpServer: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useDeleteMcpServer: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  // misc hooks required by other components
  useAdoptable: vi.fn(() => ({ data: { candidates: [] } })),
  useAdoptSession: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useSessionsViewing: vi.fn(),
  useAssessDispatch: vi.fn(() => ({ assess: vi.fn() })),
}))

// ── sessionsViewing module (used by AutomationView) ──────────────────────────
vi.mock('@/api/sessionsViewing', () => ({
  setSessionsViewing: vi.fn(),
}))

import { SettingsView } from '../SettingsView'

/** Surfaces the URL so the path-routed section strip can be asserted. */
function UrlProbe() {
  const { pathname, search } = useLocation()
  return <div data-testid="url">{pathname + search}</div>
}

function renderView(path = '/settings', section?: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <SettingsView section={section} />
        <UrlProbe />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('SettingsView', () => {
  it('defaults to the General section', async () => {
    renderView()
    expect(await screen.findByLabelText(/default tool approvals/i)).toBeInTheDocument()
  })

  it('honors ?section= deep links', async () => {
    renderView('/settings?section=plugins')
    expect(await screen.findByText('alpha')).toBeInTheDocument()
  })

  it('switches sections via the strip', async () => {
    renderView()
    await userEvent.click(screen.getByRole('tab', { name: /skills/i }))
    expect(await screen.findByText('my-skill')).toBeInTheDocument()
  })

  // ── DEC-14: the section can arrive as a path segment ──────────────────────

  it('honors a section handed in as a prop (/settings/:section)', async () => {
    renderView('/settings/skills', 'skills')
    expect(await screen.findByText('my-skill')).toBeInTheDocument()
  })

  it('lets the path section win over a stale ?section=', async () => {
    // Both channels present and disagreeing. The path is the canonical one, so
    // the query must not decide — otherwise /settings/skills?section=plugins
    // renders Plugins and the URL lies about what is on screen.
    renderView('/settings/skills?section=plugins', 'skills')
    expect(await screen.findByText('my-skill')).toBeInTheDocument()
    expect(screen.queryByText('alpha')).not.toBeInTheDocument()
  })

  it('moves the PATH when the strip is used on a path-routed Settings', async () => {
    // Writing ?section= here would be ignored by the component that just read
    // the path, leaving the tabs inert.
    renderView('/settings/general', 'general')
    await userEvent.click(screen.getByRole('tab', { name: /skills/i }))
    expect(screen.getByTestId('url')).toHaveTextContent('/settings/skills')
  })

  it('still moves ?section= when Settings is not routed by path', async () => {
    renderView('/settings')
    await userEvent.click(screen.getByRole('tab', { name: /skills/i }))
    expect(screen.getByTestId('url')).toHaveTextContent('/settings?section=skills')
  })

  it('renders all seven section tabs', () => {
    renderView()
    for (const name of [/general/i, /accounts/i, /automation/i, /plugins/i, /skills/i, /commands/i, /mcp servers/i]) {
      expect(screen.getByRole('tab', { name })).toBeInTheDocument()
    }
  })
})
