import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Dashboard } from '../Dashboard'
import * as hooks from '@/api/hooks'

vi.mock('@/api/hooks', () => ({
  useAllPrs: vi.fn(() => ({ data: [] })),
  useDaemonHealth: vi.fn(() => ({ data: { available: false } })),  // issue #434 indicator
  useGenerateSuggestions: vi.fn(() => ({ mutate: vi.fn(), mutateAsync: vi.fn(() => Promise.resolve([])), isPending: false })),
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
  useModels: vi.fn(() => ({ data: [{ id: 'default', label: 'Default' }, { id: 'opus', label: 'Opus' }, { id: 'fable', label: 'Fable' }] })),
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
  useSetAutopilotPaused: () => ({ mutate: vi.fn() }),
  useFixPr: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useAutomations: vi.fn(() => ({ data: [] })),
  usePendingDeploy: () => ({ data: { behind: false, commits: [], ci: 'green' } }),
  useDeployStatus: () => ({ data: { running: false, phases: {} } }),
  useStartDeploy: () => ({ mutate: vi.fn(), isPending: false }),
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
}))

vi.mock('@/session/useSessionSearch', () => ({
  useSessionSearch: () => ({ query: '', setQuery: vi.fn(), matchedIds: null, isFiltered: false }),
}))

const NOOP_MUTATION = {
  mutate: vi.fn(),
  isPending: false,
}

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

describe('SessionActions (via Dashboard)', () => {
  beforeEach(() => {
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)

    vi.mocked(hooks.useSessions).mockReturnValue({
      data: [
        {
          id: 'sess-1',
          name: 'Alpha session',
          accountName: 'alice',
          status: 'in_progress' as const,
          createdAt: '2026-06-19T10:00:00Z',
          updatedAt: '2026-06-19T10:00:00Z',
        },
      ],
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
      data: { defaultPermissionMode: 'interactive', thinkingCollapsed: false, repoRoot: null },
      isLoading: false,
    } as ReturnType<typeof hooks.useSettings>)

    vi.mocked(hooks.useUpdateSettings).mockReturnValue({
      mutate: vi.fn(),
      isPending: false,
      isSuccess: false,
    } as unknown as ReturnType<typeof hooks.useUpdateSettings>)

    vi.mocked(hooks.useReauthAccount).mockReturnValue({ mutate: vi.fn(), isPending: false } as unknown as ReturnType<typeof hooks.useReauthAccount>)
    vi.mocked(hooks.useReauthStatus).mockReturnValue({ data: { state: 'idle', message: null, startedAt: null }, isLoading: false } as ReturnType<typeof hooks.useReauthStatus>)
    vi.mocked(hooks.useCancelReauth).mockReturnValue({ mutate: vi.fn(), isPending: false } as unknown as ReturnType<typeof hooks.useCancelReauth>)
    vi.mocked(hooks.useArchivedSessions).mockReturnValue({ data: [], isLoading: false } as ReturnType<typeof hooks.useArchivedSessions>)
    vi.mocked(hooks.useUnarchiveSession).mockReturnValue({ mutate: vi.fn(), isPending: false } as unknown as ReturnType<typeof hooks.useUnarchiveSession>)
    vi.mocked(hooks.usePermanentDeleteSession).mockReturnValue({ mutate: vi.fn(), isPending: false } as unknown as ReturnType<typeof hooks.usePermanentDeleteSession>)
    vi.mocked(hooks.useBulkUnarchiveSessions).mockReturnValue({ mutate: vi.fn(), isPending: false } as unknown as ReturnType<typeof hooks.useBulkUnarchiveSessions>)
    vi.mocked(hooks.useBulkPermanentDeleteSessions).mockReturnValue({ mutate: vi.fn(), isPending: false } as unknown as ReturnType<typeof hooks.useBulkPermanentDeleteSessions>)
  })

  it('renders the "+ New" button', () => {
    render(<Dashboard />, { wrapper })
    expect(screen.getByRole('button', { name: /new session/i })).toBeInTheDocument()
  })

  it('opens a New Session dialog when "+ New" is clicked', async () => {
    const user = userEvent.setup()
    render(<Dashboard />, { wrapper })

    await user.click(screen.getByRole('button', { name: /new session/i }))

    const dialog = screen.getByRole('dialog', { name: /new session/i })
    expect(dialog).toBeInTheDocument()
    // Scope to the dialog: the session cards behind it now expose a "Rename …"
    // button whose accessible name also contains "name".
    expect(within(dialog).getByLabelText(/name/i)).toBeInTheDocument()
  })

  it('closes the dialog when Cancel is clicked', async () => {
    const user = userEvent.setup()
    render(<Dashboard />, { wrapper })

    await user.click(screen.getByRole('button', { name: /new session/i }))
    expect(screen.getByRole('dialog', { name: /new session/i })).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /cancel/i }))
    await waitFor(() => {
      expect(screen.queryByRole('dialog', { name: /new session/i })).not.toBeInTheDocument()
    })
  })

  it('closes the dialog when Escape is pressed', async () => {
    const user = userEvent.setup()
    render(<Dashboard />, { wrapper })

    await user.click(screen.getByRole('button', { name: /new session/i }))
    expect(screen.getByRole('dialog', { name: /new session/i })).toBeInTheDocument()

    await user.keyboard('{Escape}')
    await waitFor(() => {
      expect(screen.queryByRole('dialog', { name: /new session/i })).not.toBeInTheDocument()
    })
  })

  it('exposes rename, archive, and delete buttons directly on each session', () => {
    render(<Dashboard />, { wrapper })

    expect(screen.getByRole('button', { name: /rename session alpha session/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /archive session alpha session/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /delete session alpha session/i })).toBeInTheDocument()
  })

  it('no longer renders an overflow actions menu', () => {
    render(<Dashboard />, { wrapper })

    expect(screen.queryByRole('button', { name: /actions for session/i })).not.toBeInTheDocument()
  })

  it('clicking rename shows a rename dialog', async () => {
    const user = userEvent.setup()
    render(<Dashboard />, { wrapper })

    await user.click(screen.getByRole('button', { name: /rename session alpha session/i }))

    expect(screen.getByRole('dialog', { name: /rename session/i })).toBeInTheDocument()
  })

  it('clicking delete shows a permanent-delete confirmation dialog', async () => {
    const user = userEvent.setup()
    render(<Dashboard />, { wrapper })

    await user.click(screen.getByRole('button', { name: /delete session alpha session/i }))

    expect(screen.getByRole('dialog', { name: /permanently delete session/i })).toBeInTheDocument()
  })

  it('confirming delete calls the permanent-delete mutation', async () => {
    const permanentDelete = vi.fn()
    vi.mocked(hooks.usePermanentDeleteSession).mockReturnValue({
      mutate: permanentDelete,
      isPending: false,
    } as unknown as ReturnType<typeof hooks.usePermanentDeleteSession>)

    const user = userEvent.setup()
    render(<Dashboard />, { wrapper })

    await user.click(screen.getByRole('button', { name: /delete session alpha session/i }))
    await user.click(screen.getByRole('button', { name: /delete permanently/i }))

    expect(permanentDelete).toHaveBeenCalledWith('sess-1', expect.anything())
  })
})
