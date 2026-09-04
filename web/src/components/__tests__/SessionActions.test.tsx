import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderApp } from '@/test/renderApp'
import * as hooks from '@/api/hooks'
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
  useAssessDispatch: vi.fn(() => ({ assess: vi.fn() })),
  useResumeInterrupted: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
}))

vi.mock('@/session/useSessionSearch', () => ({
  useSessionSearch: () => ({ query: '', setQuery: vi.fn(), matchedIds: null, isFiltered: false }),
}))

const NOOP_MUTATION = {
  mutate: vi.fn(),
  isPending: false,
}

describe('session-card actions (via AppShell)', () => {
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

    vi.mocked(hooks.useUpdateAccount).mockReturnValue({
      ...NOOP_MUTATION,
      mutate: vi.fn(),
    } as unknown as ReturnType<typeof hooks.useUpdateAccount>)

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
    vi.mocked(hooks.useReauthStatus).mockReturnValue(queryResult({ state: 'idle', message: null, startedAt: null }))
    vi.mocked(hooks.useCancelReauth).mockReturnValue({ mutate: vi.fn(), isPending: false } as unknown as ReturnType<typeof hooks.useCancelReauth>)
    vi.mocked(hooks.useArchivedSessions).mockReturnValue(queryResult([]))
    vi.mocked(hooks.useUnarchiveSession).mockReturnValue({ mutate: vi.fn(), isPending: false } as unknown as ReturnType<typeof hooks.useUnarchiveSession>)
    vi.mocked(hooks.usePermanentDeleteSession).mockReturnValue({ mutate: vi.fn(), isPending: false } as unknown as ReturnType<typeof hooks.usePermanentDeleteSession>)
    vi.mocked(hooks.useBulkUnarchiveSessions).mockReturnValue({ mutate: vi.fn(), isPending: false } as unknown as ReturnType<typeof hooks.useBulkUnarchiveSessions>)
    vi.mocked(hooks.useBulkPermanentDeleteSessions).mockReturnValue({ mutate: vi.fn(), isPending: false } as unknown as ReturnType<typeof hooks.useBulkPermanentDeleteSessions>)
  })

  // Task 18: the four "+ New opens/closes the New Session dialog" cases that
  // opened this file are re-pinned in `App.dialogs.test.tsx`, which now covers
  // opening from `?dialog=new` in every layout mode, opening from the rail's
  // link, Cancel, and the param clearing without a history push. The New Session
  // control on the list column is pinned by `SessionListColumn.test.tsx`
  // ("marks the New Session button when the new-session slot is the target").
  // What is left here is the part that is genuinely the shell's: the per-card ⋯
  // menu and the dialogs `AppShell` owns behind it.

  it('renders a `…` menu button for session actions on each card', () => {
    // Rename/Archive/Delete live in the unified `…` menu on every breakpoint.
    renderApp({ path: '/sessions', mode: 'desktop' })

    expect(screen.getAllByRole('button', { name: /more actions for session/i }).length).toBeGreaterThan(0)
  })

  it('clicking rename in the ⋯ menu shows a rename dialog', async () => {
    const user = userEvent.setup()
    renderApp({ path: '/sessions', mode: 'desktop' })

    await user.click(screen.getByRole('button', { name: /more actions for session alpha session/i }))
    await user.click(screen.getByRole('menuitem', { name: /rename/i }))

    expect(screen.getByRole('dialog', { name: /rename session/i })).toBeInTheDocument()
  })

  it('clicking delete in the ⋯ menu shows a permanent-delete confirmation dialog', async () => {
    const user = userEvent.setup()
    renderApp({ path: '/sessions', mode: 'desktop' })

    await user.click(screen.getByRole('button', { name: /more actions for session alpha session/i }))
    await user.click(screen.getByRole('menuitem', { name: /delete/i }))

    expect(screen.getByRole('dialog', { name: /permanently delete session/i })).toBeInTheDocument()
  })

  it('confirming delete calls the permanent-delete mutation', async () => {
    const permanentDelete = vi.fn()
    vi.mocked(hooks.usePermanentDeleteSession).mockReturnValue({
      mutate: permanentDelete,
      isPending: false,
    } as unknown as ReturnType<typeof hooks.usePermanentDeleteSession>)

    const user = userEvent.setup()
    renderApp({ path: '/sessions', mode: 'desktop' })

    await user.click(screen.getByRole('button', { name: /more actions for session alpha session/i }))
    await user.click(screen.getByRole('menuitem', { name: /delete/i }))
    await user.click(screen.getByRole('button', { name: /delete permanently/i }))

    expect(permanentDelete).toHaveBeenCalledWith('sess-1', expect.anything())
  })
})
