import { describe, it, expect, vi, beforeEach } from 'vitest'
import { cleanup, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { NewSessionDialog } from '@/features/sessions/NewSessionDialog'
import * as hooks from '@/api/hooks'
import { appSettings, githubRepo } from '@/test/factories'
import { queryResult } from '@/test/reactQuery'

vi.mock('@/api/hooks', () => ({
  useQueueMove: vi.fn(() => ({ mutate: vi.fn() })),
  useQueueCancel: vi.fn(() => ({ mutate: vi.fn() })),
  useModelApproval: vi.fn(() => ({ mutate: vi.fn() })),
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
  useUpdateAccount: vi.fn(),
  useDeleteAccount: vi.fn(),
  useSetAccountEnabled: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
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
  useBulkArchiveSessions: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
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
  useAssessDispatch: vi.fn(() => ({ assess: vi.fn() })),
  useResumeInterrupted: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useRotateStalled: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
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
  vi.mocked(hooks.useAccounts).mockReturnValue(queryResult([]))
  vi.mocked(hooks.useSessions).mockReturnValue(queryResult([]))
  vi.mocked(hooks.useCreateSession).mockReturnValue(NOOP as unknown as ReturnType<typeof hooks.useCreateSession>)
  vi.mocked(hooks.useRenameSession).mockReturnValue(NOOP as unknown as ReturnType<typeof hooks.useRenameSession>)
  vi.mocked(hooks.useCloseSession).mockReturnValue(NOOP as unknown as ReturnType<typeof hooks.useCloseSession>)
  vi.mocked(hooks.useRefreshAccount).mockReturnValue(NOOP as unknown as ReturnType<typeof hooks.useRefreshAccount>)
  vi.mocked(hooks.useUpdateAccount).mockReturnValue(NOOP as unknown as ReturnType<typeof hooks.useUpdateAccount>)
  vi.mocked(hooks.useDeleteAccount).mockReturnValue(NOOP as unknown as ReturnType<typeof hooks.useDeleteAccount>)
  // One repo whose default branch is "main".
  vi.mocked(hooks.useGithubRepos).mockReturnValue(queryResult([githubRepo({ nameWithOwner: 'owner/repo' })]))
  // Branches resolve with "main" as the default.
  vi.mocked(hooks.useGithubBranches).mockReturnValue(queryResult({ default: 'main', branches: ['main', 'dev'] }))
  vi.mocked(hooks.useSettings).mockReturnValue(queryResult(appSettings()))
  vi.mocked(hooks.useUpdateSettings).mockReturnValue(NOOP as unknown as ReturnType<typeof hooks.useUpdateSettings>)
  vi.mocked(hooks.useReauthAccount).mockReturnValue(NOOP as unknown as ReturnType<typeof hooks.useReauthAccount>)
  vi.mocked(hooks.useReauthStatus).mockReturnValue(queryResult({ state: 'idle', message: null, startedAt: null }))
  vi.mocked(hooks.useCancelReauth).mockReturnValue(NOOP as unknown as ReturnType<typeof hooks.useCancelReauth>)
  vi.mocked(hooks.useArchivedSessions).mockReturnValue(queryResult([]))
  vi.mocked(hooks.useUnarchiveSession).mockReturnValue(NOOP as unknown as ReturnType<typeof hooks.useUnarchiveSession>)
  vi.mocked(hooks.usePermanentDeleteSession).mockReturnValue(NOOP as unknown as ReturnType<typeof hooks.usePermanentDeleteSession>)
  vi.mocked(hooks.useBulkUnarchiveSessions).mockReturnValue(NOOP as unknown as ReturnType<typeof hooks.useBulkUnarchiveSessions>)
  vi.mocked(hooks.useBulkPermanentDeleteSessions).mockReturnValue(NOOP as unknown as ReturnType<typeof hooks.useBulkPermanentDeleteSessions>)
}

/**
 * The submit payload the dialog builds. It used to be read off
 * `useCreateSession().mutate`, because `Dashboard` was the only way to open the
 * dialog; the dialog's own contract is `onSubmit(input)`, and the half that
 * hands that to the mutation now lives in `NewSessionDialogHost` (and is pinned
 * there). Nothing is lost: `expect.not.objectContaining` still runs, on the
 * value the dialog actually produces.
 */
const onSubmit = vi.fn()

function openDialog() {
  render(
    <NewSessionDialog accounts={[]} onClose={vi.fn()} onSubmit={onSubmit} isPending={false} />,
    { wrapper },
  )
  return within(screen.getByRole('dialog', { name: /new session/i }))
}

describe('New Session dialog — branch default selection', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    setupMocks()
  })

  it('defaults the branch select to the loaded default branch after a repo is chosen', async () => {
    const user = userEvent.setup()
    const dialog = openDialog()

    // Choose a repo → branches load with default "main".
    await user.selectOptions(dialog.getByLabelText(/^repository/i), 'owner/repo')

    const branchSelect = dialog.getByLabelText(/^branch/i) as HTMLSelectElement
    expect(branchSelect.value).toBe('main')
  })

  it('lets the user override the default branch', async () => {
    const user = userEvent.setup()
    const dialog = openDialog()
    await user.selectOptions(dialog.getByLabelText(/^repository/i), 'owner/repo')

    const branchSelect = dialog.getByLabelText(/^branch/i) as HTMLSelectElement
    await user.selectOptions(branchSelect, 'dev')
    expect(branchSelect.value).toBe('dev')
  })

  it('defaults the model select to "Default" and offers Fable', async () => {
    const dialog = openDialog()

    const modelSelect = dialog.getByLabelText(/^model/i) as HTMLSelectElement
    expect(modelSelect.value).toBe('default')
    expect(within(modelSelect).getByRole('option', { name: 'Fable' })).toBeInTheDocument()
  })

  it('groups the repo chooser, account, and model under a "Launch target" fieldset', async () => {
    const dialog = openDialog()

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
    const dialog = openDialog()

    const model = dialog.getByLabelText(/^model/i)
    const toolApprovals = dialog.getByLabelText(/tool approvals/i)
    // Model was previously buried below Tool approvals; it now precedes it.
    expect(model.compareDocumentPosition(toolApprovals) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('never preselects a gated (fable) tier as the default model', async () => {
    const dialog = openDialog()

    const modelSelect = dialog.getByLabelText(/^model/i) as HTMLSelectElement
    // fable is a gated tier — it must not be the default selected value
    expect(modelSelect.value).not.toBe('fable')
  })

  it('shows an approval note when a gated (fable) model is selected', async () => {
    const user = userEvent.setup()
    const dialog = openDialog()

    // No note before selecting fable
    expect(dialog.queryByText(/requires approval/i)).not.toBeInTheDocument()

    await user.selectOptions(dialog.getByLabelText(/^model/i), 'fable')
    expect(dialog.getByText(/requires approval/i)).toBeInTheDocument()
  })

  it('hides the approval note when a non-gated model is selected', async () => {
    const user = userEvent.setup()
    const dialog = openDialog()

    await user.selectOptions(dialog.getByLabelText(/^model/i), 'fable')
    expect(dialog.getByText(/requires approval/i)).toBeInTheDocument()

    await user.selectOptions(dialog.getByLabelText(/^model/i), 'opus')
    expect(dialog.queryByText(/requires approval/i)).not.toBeInTheDocument()
  })

  it('submits the chosen model, and omits it when left on "Default"', async () => {
    const user = userEvent.setup()

    // Default → model omitted from the payload (account default).
    const first = openDialog()
    await user.click(first.getByRole('button', { name: /^create$/i }))
    expect(onSubmit).toHaveBeenLastCalledWith(
      expect.not.objectContaining({ model: expect.anything() }),
    )

    // A fresh dialog, because the model select is local state — the same reset
    // the old close/reopen through the shell produced.
    cleanup()

    // Pick Fable → forwarded as model: 'fable'.
    const second = openDialog()
    await user.selectOptions(second.getByLabelText(/^model/i), 'fable')
    await user.click(second.getByRole('button', { name: /^create$/i }))
    expect(onSubmit).toHaveBeenLastCalledWith(
      expect.objectContaining({ model: 'fable' }),
    )
  })
})
