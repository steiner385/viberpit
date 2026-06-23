import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Dashboard } from '../Dashboard'
import * as hooks from '@/api/hooks'

vi.mock('@/api/hooks', () => ({
  useConfig: vi.fn(() => ({ data: { adoptionEnabled: false } })),
  useAdoptable: vi.fn(() => ({ data: { candidates: [] } })),
  useAdoptSession: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useAccounts: vi.fn(),
  useSessions: vi.fn(),
  useCreateSession: vi.fn(),
  useRenameSession: vi.fn(),
  useCloseSession: vi.fn(),
  useRefreshAccount: vi.fn(),
  useUpdateAccountNickname: vi.fn(),
  useDeleteAccount: vi.fn(),
  useGithubRepos: vi.fn(),
  useGithubBranches: vi.fn(),
  useSettings: vi.fn(),
  useUpdateSettings: vi.fn(),
  useReauthAccount: vi.fn(),
  useReauthStatus: vi.fn(),
  useCancelReauth: vi.fn(),
  useArchivedSessions: vi.fn(),
  useUnarchiveSession: vi.fn(),
  usePermanentDeleteSession: vi.fn(),
  useBulkUnarchiveSessions: vi.fn(),
  useBulkPermanentDeleteSessions: vi.fn(),
  useSendPrompt: () => ({ mutate: vi.fn() }),
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

const NOOP_MUTATION = {
  mutate: vi.fn(),
  isPending: false,
  isSuccess: false,
}

const SETTINGS = {
  defaultPermissionMode: 'auto',
  thinkingCollapsed: false,
  repoRoot: null,
}

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

describe('Dashboard — settings integration', () => {
  beforeEach(() => {
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)

    vi.mocked(hooks.useSessions).mockReturnValue({
      data: [],
      isLoading: false,
    } as ReturnType<typeof hooks.useSessions>)

    vi.mocked(hooks.useCreateSession).mockReturnValue({
      ...NOOP_MUTATION,
      mutate: vi.fn(),
    } as unknown as ReturnType<typeof hooks.useCreateSession>)

    vi.mocked(hooks.useRenameSession).mockReturnValue({
      ...NOOP_MUTATION,
      mutate: vi.fn(),
    } as unknown as ReturnType<typeof hooks.useRenameSession>)

    vi.mocked(hooks.useCloseSession).mockReturnValue({
      ...NOOP_MUTATION,
      mutate: vi.fn(),
    } as unknown as ReturnType<typeof hooks.useCloseSession>)

    vi.mocked(hooks.useRefreshAccount).mockReturnValue({
      ...NOOP_MUTATION,
      mutate: vi.fn(),
    } as unknown as ReturnType<typeof hooks.useRefreshAccount>)

    vi.mocked(hooks.useUpdateAccountNickname).mockReturnValue({
      ...NOOP_MUTATION,
      mutate: vi.fn(),
    } as unknown as ReturnType<typeof hooks.useUpdateAccountNickname>)

    vi.mocked(hooks.useDeleteAccount).mockReturnValue({
      ...NOOP_MUTATION,
      mutate: vi.fn(),
    } as unknown as ReturnType<typeof hooks.useDeleteAccount>)

    vi.mocked(hooks.useGithubRepos).mockReturnValue({
      data: [],
      isLoading: false,
      isError: false,
    } as ReturnType<typeof hooks.useGithubRepos>)

    vi.mocked(hooks.useGithubBranches).mockReturnValue({
      data: { default: null, branches: [] },
      isLoading: false,
    } as ReturnType<typeof hooks.useGithubBranches>)

    vi.mocked(hooks.useSettings).mockReturnValue({
      data: SETTINGS,
      isLoading: false,
    } as ReturnType<typeof hooks.useSettings>)

    vi.mocked(hooks.useUpdateSettings).mockReturnValue({
      ...NOOP_MUTATION,
      mutate: vi.fn(),
    } as unknown as ReturnType<typeof hooks.useUpdateSettings>)

    vi.mocked(hooks.useReauthAccount).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useReauthAccount>)
    vi.mocked(hooks.useReauthStatus).mockReturnValue({ data: { state: 'idle', message: null, startedAt: null }, isLoading: false } as ReturnType<typeof hooks.useReauthStatus>)
    vi.mocked(hooks.useCancelReauth).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useCancelReauth>)
    vi.mocked(hooks.useArchivedSessions).mockReturnValue({ data: [], isLoading: false } as ReturnType<typeof hooks.useArchivedSessions>)
    vi.mocked(hooks.useUnarchiveSession).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useUnarchiveSession>)
    vi.mocked(hooks.usePermanentDeleteSession).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.usePermanentDeleteSession>)
    vi.mocked(hooks.useBulkUnarchiveSessions).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useBulkUnarchiveSessions>)
    vi.mocked(hooks.useBulkPermanentDeleteSessions).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useBulkPermanentDeleteSessions>)
  })

  // ── Settings button ──────────────────────────────────────────────────────

  it('renders a Settings button', () => {
    render(<Dashboard />, { wrapper })
    expect(screen.getByRole('button', { name: /settings/i })).toBeInTheDocument()
  })

  it('opens the Settings dialog when the Settings button is clicked', async () => {
    const user = userEvent.setup()
    render(<Dashboard />, { wrapper })

    await user.click(screen.getByRole('button', { name: /settings/i }))

    expect(screen.getByRole('dialog', { name: /settings/i })).toBeInTheDocument()
  })

  it('closes the Settings dialog when Close is clicked inside it', async () => {
    const user = userEvent.setup()
    render(<Dashboard />, { wrapper })

    await user.click(screen.getByRole('button', { name: /settings/i }))
    expect(screen.getByRole('dialog', { name: /settings/i })).toBeInTheDocument()

    // The SettingsPanel's close button (not Cancel — it's labeled "Close")
    await user.click(screen.getByRole('button', { name: /^close$/i }))

    await waitFor(() => {
      expect(screen.queryByRole('dialog', { name: /settings/i })).not.toBeInTheDocument()
    })
  })

  // ── New session dialog — Tool approvals ────────────────────────────────

  it('shows a Tool approvals select in the New Session dialog', async () => {
    const user = userEvent.setup()
    render(<Dashboard />, { wrapper })

    // Use the header "+ New" button (aria-label="New session"), not EmptyState
    await user.click(screen.getAllByRole('button', { name: /new session/i })[0])

    expect(screen.getByLabelText(/tool approvals/i)).toBeInTheDocument()
  })

  it('defaults the Tool approvals select to the settings default', async () => {
    const user = userEvent.setup()
    render(<Dashboard />, { wrapper })

    await user.click(screen.getAllByRole('button', { name: /new session/i })[0])

    const select = screen.getByLabelText(/tool approvals/i) as HTMLSelectElement
    // SETTINGS.defaultPermissionMode = 'auto'
    expect(select.value).toBe('auto')
  })

  it('falls back to "interactive" when settings data is not yet loaded', async () => {
    vi.mocked(hooks.useSettings).mockReturnValue({
      data: undefined,
      isLoading: true,
    } as ReturnType<typeof hooks.useSettings>)

    const user = userEvent.setup()
    render(<Dashboard />, { wrapper })

    await user.click(screen.getAllByRole('button', { name: /new session/i })[0])

    const select = screen.getByLabelText(/tool approvals/i) as HTMLSelectElement
    expect(select.value).toBe('interactive')
  })

  it('submits permissionMode in the create session payload', async () => {
    const mutate = vi.fn()
    vi.mocked(hooks.useCreateSession).mockReturnValue({
      ...NOOP_MUTATION,
      mutate,
    } as unknown as ReturnType<typeof hooks.useCreateSession>)

    const user = userEvent.setup()
    render(<Dashboard />, { wrapper })

    await user.click(screen.getAllByRole('button', { name: /new session/i })[0])
    // Change to 'full-auto'
    await user.selectOptions(screen.getByLabelText(/tool approvals/i), 'full-auto')
    await user.click(screen.getByRole('button', { name: /^create$/i }))

    expect(mutate).toHaveBeenCalledWith(
      expect.objectContaining({ permissionMode: 'full-auto' }),
      expect.anything(),
    )
  })
})
