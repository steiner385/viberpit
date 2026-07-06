import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, within } from '@testing-library/react'
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
  useSetAutopilotPaused: () => ({ mutate: vi.fn() }),
  useFixPr: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useAutomations: vi.fn(() => ({ data: [] })),
  useReauthAccount: vi.fn(),
  useReauthStatus: vi.fn(),
  useCancelReauth: vi.fn(),
  useArchivedSessions: vi.fn(),
  useUnarchiveSession: vi.fn(),
  usePermanentDeleteSession: vi.fn(),
  useBulkUnarchiveSessions: vi.fn(),
  useBulkPermanentDeleteSessions: vi.fn(),
  useSendPrompt: () => ({ mutate: vi.fn() }),
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

const NOOP = { mutate: vi.fn(), isPending: false, isSuccess: false }

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

function setupMocks() {
  vi.mocked(hooks.useAccounts).mockReturnValue({ data: [], isLoading: false } as ReturnType<typeof hooks.useAccounts>)
  vi.mocked(hooks.useSessions).mockReturnValue({ data: [], isLoading: false } as ReturnType<typeof hooks.useSessions>)
  vi.mocked(hooks.useCreateSession).mockReturnValue(NOOP as unknown as ReturnType<typeof hooks.useCreateSession>)
  vi.mocked(hooks.useRenameSession).mockReturnValue(NOOP as unknown as ReturnType<typeof hooks.useRenameSession>)
  vi.mocked(hooks.useCloseSession).mockReturnValue(NOOP as unknown as ReturnType<typeof hooks.useCloseSession>)
  vi.mocked(hooks.useRefreshAccount).mockReturnValue(NOOP as unknown as ReturnType<typeof hooks.useRefreshAccount>)
  vi.mocked(hooks.useUpdateAccountNickname).mockReturnValue(NOOP as unknown as ReturnType<typeof hooks.useUpdateAccountNickname>)
  vi.mocked(hooks.useDeleteAccount).mockReturnValue(NOOP as unknown as ReturnType<typeof hooks.useDeleteAccount>)
  // One repo whose default branch is "main".
  vi.mocked(hooks.useGithubRepos).mockReturnValue({
    data: [{ nameWithOwner: 'owner/repo', defaultBranch: 'main', isPrivate: false, cloned: true }],
    isLoading: false,
    isError: false,
  } as ReturnType<typeof hooks.useGithubRepos>)
  // Branches resolve with "main" as the default.
  vi.mocked(hooks.useGithubBranches).mockReturnValue({
    data: { default: 'main', branches: ['main', 'dev'] },
    isLoading: false,
  } as ReturnType<typeof hooks.useGithubBranches>)
  vi.mocked(hooks.useSettings).mockReturnValue({
    data: { defaultPermissionMode: 'interactive', thinkingCollapsed: false, repoRoot: null },
    isLoading: false,
  } as ReturnType<typeof hooks.useSettings>)
  vi.mocked(hooks.useUpdateSettings).mockReturnValue(NOOP as unknown as ReturnType<typeof hooks.useUpdateSettings>)
  vi.mocked(hooks.useReauthAccount).mockReturnValue(NOOP as unknown as ReturnType<typeof hooks.useReauthAccount>)
  vi.mocked(hooks.useReauthStatus).mockReturnValue({ data: { state: 'idle', message: null, startedAt: null }, isLoading: false } as ReturnType<typeof hooks.useReauthStatus>)
  vi.mocked(hooks.useCancelReauth).mockReturnValue(NOOP as unknown as ReturnType<typeof hooks.useCancelReauth>)
  vi.mocked(hooks.useArchivedSessions).mockReturnValue({ data: [], isLoading: false } as ReturnType<typeof hooks.useArchivedSessions>)
  vi.mocked(hooks.useUnarchiveSession).mockReturnValue(NOOP as unknown as ReturnType<typeof hooks.useUnarchiveSession>)
  vi.mocked(hooks.usePermanentDeleteSession).mockReturnValue(NOOP as unknown as ReturnType<typeof hooks.usePermanentDeleteSession>)
  vi.mocked(hooks.useBulkUnarchiveSessions).mockReturnValue(NOOP as unknown as ReturnType<typeof hooks.useBulkUnarchiveSessions>)
  vi.mocked(hooks.useBulkPermanentDeleteSessions).mockReturnValue(NOOP as unknown as ReturnType<typeof hooks.useBulkPermanentDeleteSessions>)
}

describe('New Session dialog — branch default selection', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    setupMocks()
  })

  it('defaults the branch select to the loaded default branch after a repo is chosen', async () => {
    const user = userEvent.setup()
    render(<Dashboard />, { wrapper })

    await user.click(screen.getAllByRole('button', { name: /new session/i })[0])
    const dialog = within(screen.getByRole('dialog', { name: /new session/i }))

    // Choose a repo → branches load with default "main".
    await user.selectOptions(dialog.getByLabelText(/^repository/i), 'owner/repo')

    const branchSelect = dialog.getByLabelText(/^branch/i) as HTMLSelectElement
    expect(branchSelect.value).toBe('main')
  })

  it('lets the user override the default branch', async () => {
    const user = userEvent.setup()
    render(<Dashboard />, { wrapper })

    await user.click(screen.getAllByRole('button', { name: /new session/i })[0])
    const dialog = within(screen.getByRole('dialog', { name: /new session/i }))
    await user.selectOptions(dialog.getByLabelText(/^repository/i), 'owner/repo')

    const branchSelect = dialog.getByLabelText(/^branch/i) as HTMLSelectElement
    await user.selectOptions(branchSelect, 'dev')
    expect(branchSelect.value).toBe('dev')
  })

  it('defaults the model select to "Default" and offers Fable', async () => {
    const user = userEvent.setup()
    render(<Dashboard />, { wrapper })

    await user.click(screen.getAllByRole('button', { name: /new session/i })[0])
    const dialog = within(screen.getByRole('dialog', { name: /new session/i }))

    const modelSelect = dialog.getByLabelText(/^model/i) as HTMLSelectElement
    expect(modelSelect.value).toBe('default')
    expect(within(modelSelect).getByRole('option', { name: 'Fable' })).toBeInTheDocument()
  })

  it('groups the repo chooser, account, and model under a "Launch target" fieldset', async () => {
    const user = userEvent.setup()
    render(<Dashboard />, { wrapper })

    await user.click(screen.getAllByRole('button', { name: /new session/i })[0])
    const dialog = within(screen.getByRole('dialog', { name: /new session/i }))

    // The launch-config controls live together in one accessible group.
    const group = within(dialog.getByRole('group', { name: /launch target/i }))
    expect(group.getByLabelText(/^repository/i)).toBeInTheDocument()
    expect(group.getByLabelText(/^branch/i)).toBeInTheDocument()
    expect(group.getByLabelText(/^account/i)).toBeInTheDocument()
    expect(group.getByLabelText(/^model/i)).toBeInTheDocument()

    // Tool approvals is a separate control, outside the launch-target group.
    expect(group.queryByLabelText(/tool approvals/i)).not.toBeInTheDocument()
  })

  it('places the model select before "Tool approvals" in DOM order', async () => {
    const user = userEvent.setup()
    render(<Dashboard />, { wrapper })

    await user.click(screen.getAllByRole('button', { name: /new session/i })[0])
    const dialog = within(screen.getByRole('dialog', { name: /new session/i }))

    const model = dialog.getByLabelText(/^model/i)
    const toolApprovals = dialog.getByLabelText(/tool approvals/i)
    // Model was previously buried below Tool approvals; it now precedes it.
    expect(model.compareDocumentPosition(toolApprovals) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('submits the chosen model, and omits it when left on "Default"', async () => {
    const user = userEvent.setup()
    render(<Dashboard />, { wrapper })

    // Default → model omitted from the payload (account default).
    await user.click(screen.getAllByRole('button', { name: /new session/i })[0])
    let dialog = within(screen.getByRole('dialog', { name: /new session/i }))
    await user.click(dialog.getByRole('button', { name: /^create$/i }))
    expect(NOOP.mutate).toHaveBeenLastCalledWith(
      expect.not.objectContaining({ model: expect.anything() }),
      expect.anything(),
    )

    // Pick Fable → forwarded as model: 'fable'.
    await user.click(screen.getAllByRole('button', { name: /new session/i })[0])
    dialog = within(screen.getByRole('dialog', { name: /new session/i }))
    await user.selectOptions(dialog.getByLabelText(/^model/i), 'fable')
    await user.click(dialog.getByRole('button', { name: /^create$/i }))
    expect(NOOP.mutate).toHaveBeenLastCalledWith(
      expect.objectContaining({ model: 'fable' }),
      expect.anything(),
    )
  })
})
