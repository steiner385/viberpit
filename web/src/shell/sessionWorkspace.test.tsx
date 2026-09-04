/**
 * The Sessions workspace the shell owns — `useSessionsWorkspace` in `AppShell`.
 *
 * These are the cases that were about the SHELL rather than about a region: the
 * destructive-action dialogs it hosts, the advance-to-neighbour that follows
 * them, the list's grouping control, and the all-limited banner's lifetime. They
 * came from `DesktopShell.test.tsx`, `DesktopAdvanceOnArchive.test.tsx` and
 * `DashboardTabs.test.tsx`, each of which had to mount a whole shell to reach
 * them — as does this file, which is the point: there is one shell now.
 *
 * Related coverage lives with its own subject and is not repeated here:
 * `useArchiveAdvance.test.tsx` (the session-route half of advance-on-archive),
 * `SessionListColumn.test.tsx` (what the list renders), `PromptDock.test.tsx`
 * (the cursor the prompt and the list share).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderApp } from '@/test/renderApp'
import * as hooks from '@/api/hooks'
import { ApiRequestError } from '@/api/client'
import type { Account, Session } from '@/api/types'
import { loadingQueryResult, queryResult } from '@/test/reactQuery'
import { getServerSttEnabled, setServerSttEnabled } from '@/lib/sttCapability'

const NOOP = { mutate: vi.fn(), isPending: false, isSuccess: false }

vi.mock('@/api/hooks', () => ({
  useQueueMove: vi.fn(() => ({ mutate: vi.fn() })),
  useQueueCancel: vi.fn(() => ({ mutate: vi.fn() })),
  useModelApproval: vi.fn(() => ({ mutate: vi.fn() })),
  useAllPrs: vi.fn(() => ({ data: [] })),
  useDaemonHealth: vi.fn(() => ({ data: { available: false } })),
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
  useGithubRepos: vi.fn(() => ({ data: [], isLoading: false, isError: false })),
  useModels: vi.fn(() => ({ data: [] })),
  useGithubBranches: vi.fn(() => ({ data: { default: null, branches: [] }, isLoading: false })),
  useSettings: vi.fn(() => ({ data: { defaultPermissionMode: 'interactive', thinkingCollapsed: false, repoRoot: null }, isLoading: false })),
  useUpdateSettings: vi.fn(() => NOOP),
  useSetAutopilotPaused: () => ({ mutate: vi.fn() }),
  useFixPr: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useAutomations: vi.fn(() => ({ data: [] })),
  useReauthAccount: vi.fn(() => NOOP),
  useReauthStatus: vi.fn(() => ({ data: { state: 'idle', message: null, startedAt: null }, isLoading: false })),
  useCancelReauth: vi.fn(() => NOOP),
  useArchivedSessions: vi.fn(() => ({ data: [], isLoading: false })),
  useUnarchiveSession: vi.fn(() => NOOP),
  usePermanentDeleteSession: vi.fn(),
  useBulkUnarchiveSessions: vi.fn(() => NOOP),
  useBulkPermanentDeleteSessions: vi.fn(() => NOOP),
  useBulkArchiveSessions: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useSendPrompt: vi.fn(() => ({ mutate: vi.fn() })),
  usePendingDeploy: () => ({ data: { behind: false, commits: [], ci: 'green' } }),
  useDeployStatus: () => ({ data: { running: false, phases: {} } }),
  useStartDeploy: () => ({ mutate: vi.fn(), isPending: false }),
  useSessionsViewing: vi.fn(),
  useAddCopilotAccount: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useCopilotAddStatus: vi.fn(() => ({ data: undefined, isLoading: false })),
  useCancelCopilotAdd: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useResumeInterrupted: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useAssessDispatch: vi.fn(() => ({ assess: vi.fn() })),
  QK: {
    accounts: ['accounts'], sessions: ['sessions'], archived: ['archived-sessions'],
    config: ['config'], githubRepos: ['github-repos'],
    githubBranches: (repo?: string) => ['github-branches', repo], settings: ['settings'],
  },
}))

vi.mock('@/session/useSessionSearch', () => ({
  useSessionSearch: () => ({ query: '', setQuery: vi.fn(), matchedIds: null, isFiltered: false }),
}))

// Settled SUCCESSFULLY, so the list state's "both queries have resolved" gate
// turns on the SESSIONS query alone — which is what the load-order tests at the
// bottom of this file are about. `isSuccess` is the flag that gate reads (not
// `!isPending`, which an ERRORED fleet query also satisfies while carrying no
// data), so a stub that only says `isPending: false` describes a query that has
// given up, and the gate correctly refuses to sanitize against it.
// `useEnabledFleetHosts` is left real (it reads the module-local hook, not this
// override), exactly as before.
vi.mock('@/api/fleet', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/fleet')>()
  return {
    ...actual,
    useFleetHosts: () => ({ data: { hosts: [] }, isPending: false, isSuccess: true, isError: false }),
  }
})

vi.mock('@/ws/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/ws/client')>()
  return {
    ...actual,
    useSessionSocket: () => ({
      attach: vi.fn(), detach: vi.fn(), interrupt: vi.fn(), prompt: vi.fn(),
      sendInterrupt: vi.fn(), permissionResponse: vi.fn(), answerQuestion: vi.fn(),
      setPermissionMode: vi.fn(), cancelQueued: vi.fn(), command: vi.fn(),
      activity: vi.fn(), on: vi.fn(), off: vi.fn(),
      connectionState: 'connected' as const, socket: null,
    }),
  }
})

/** Typed factories — real values, never `as unknown as T` (R10). */
function session(over: Partial<Session> = {}): Session {
  return {
    id: 's1', name: 'One', accountName: 'alice', status: 'idle',
    createdAt: '2026-06-19T10:00:00Z', prs: [],
    ...over,
  }
}

function account(over: Partial<Account> = {}): Account {
  return {
    name: 'alice', status: 'ok',
    usage: { session: { pct: 10, resetsAt: null }, weekly: { pct: 10, resetsAt: null } },
    liveSessions: { active: 0, idle: 0 }, rank: null, rankReason: null,
    ...over,
  }
}

const TWO = [session({ id: 's1', name: 'One' }), session({ id: 's2', name: 'Two' })]

function setSessions(list: Session[]) {
  vi.mocked(hooks.useSessions).mockReturnValue(
    queryResult(list),
  )
}

function setAccounts(list: Account[]) {
  vi.mocked(hooks.useAccounts).mockReturnValue(
    queryResult(list),
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  setSessions(TWO)
  setAccounts([account()])
  vi.mocked(hooks.useCreateSession).mockReturnValue(NOOP as unknown as ReturnType<typeof hooks.useCreateSession>)
  vi.mocked(hooks.useRenameSession).mockReturnValue(NOOP as unknown as ReturnType<typeof hooks.useRenameSession>)
  vi.mocked(hooks.useCloseSession).mockReturnValue(NOOP as unknown as ReturnType<typeof hooks.useCloseSession>)
  vi.mocked(hooks.usePermanentDeleteSession).mockReturnValue(NOOP as unknown as ReturnType<typeof hooks.usePermanentDeleteSession>)
})

describe('the shell hosts the destructive-action dialogs', () => {
  it('deleting a session opens a confirm dialog; the mutation runs only on confirm', async () => {
    const user = userEvent.setup()
    const deleteMutate = vi.fn()
    vi.mocked(hooks.usePermanentDeleteSession).mockReturnValue(
      { mutate: deleteMutate, isPending: false, isSuccess: false } as unknown as ReturnType<typeof hooks.usePermanentDeleteSession>,
    )
    renderApp({ path: '/sessions', mode: 'desktop' })

    await user.click(await screen.findByRole('button', { name: /more actions for session one/i }))
    await user.click(screen.getByRole('menuitem', { name: /delete/i }))

    // Destructive: the confirm dialog appears and nothing is deleted yet.
    const dialog = screen.getByRole('dialog')
    expect(deleteMutate).not.toHaveBeenCalled()

    await user.click(within(dialog).getByRole('button', { name: /delete permanently/i }))
    expect(deleteMutate).toHaveBeenCalledWith('s1', expect.anything())
  })

  it('renaming from a list row opens the rename dialog with that session', async () => {
    const user = userEvent.setup()
    renderApp({ path: '/sessions', mode: 'desktop' })

    await user.click(await screen.findByRole('button', { name: /more actions for session two/i }))
    await user.click(screen.getByRole('menuitem', { name: /rename/i }))

    const dialog = screen.getByRole('dialog', { name: /rename session/i })
    expect(within(dialog).getByLabelText(/session name/i)).toHaveValue('Two')
  })
})

describe('the shell advances the open session when it leaves the list', () => {
  it('permanently deleting the OPEN session advances to the neighbour', async () => {
    const user = userEvent.setup()
    const deleteMutate = vi.fn((_id: string, opts?: { onSuccess?: () => void }) => opts?.onSuccess?.())
    vi.mocked(hooks.usePermanentDeleteSession).mockReturnValue(
      { mutate: deleteMutate, isPending: false, isSuccess: false } as unknown as ReturnType<typeof hooks.usePermanentDeleteSession>,
    )
    const app = renderApp({ path: '/sessions/s1', mode: 'desktop' })

    await user.click(await screen.findByRole('button', { name: /more actions for session one/i }))
    await user.click(screen.getByRole('menuitem', { name: /delete/i }))
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: /delete permanently/i }))

    await waitFor(() => expect(app.path()).toBe('/sessions/s2'))
  })

  it('archiving a session that is NOT open leaves the open one alone', async () => {
    const user = userEvent.setup()
    const closeMutate = vi.fn()
    vi.mocked(hooks.useCloseSession).mockReturnValue(
      { mutate: closeMutate, isPending: false, isSuccess: false } as unknown as ReturnType<typeof hooks.useCloseSession>,
    )
    const app = renderApp({ path: '/sessions/s1', mode: 'desktop' })

    await user.click(await screen.findByRole('button', { name: /more actions for session two/i }))
    await user.click(screen.getByRole('menuitem', { name: /archive/i }))

    expect(closeMutate).toHaveBeenCalledWith('s2')
    // `advanceFrom` bails unless the REMOVED session is the open one.
    expect(app.path()).toBe('/sessions/s1')
  })
})

describe('the shell drives the session list controls', () => {
  it('groups the session list by status when selected', async () => {
    const user = userEvent.setup()
    setSessions([
      session({ id: 's1', name: 'One', status: 'idle' }),
      session({ id: 's2', name: 'Two', status: 'in_progress' }),
    ])
    renderApp({ path: '/sessions', mode: 'desktop' })

    await user.selectOptions(await screen.findByLabelText('Group by'), 'status')
    // Two sessions from DIFFERENT tiers → both group headers must render
    // (idle → 'your-turn' tier, header "Done"; in_progress → 'busy', "Working").
    // Scoped to the scrolling list band: the Status FILTER in the controls strip
    // above it offers the same TIER_LABEL vocabulary as <option> text (one source
    // of truth for tier names), so a document-wide getByText now matches both the
    // header and the option. Same assertion, narrowed to the headers it means.
    const list = within(screen.getByTestId('session-list-scroll'))
    expect(list.getByText('Done')).toBeInTheDocument()
    expect(list.getByText('Working')).toBeInTheDocument()
  })
})

describe('the all-limited banner', () => {
  /** Raise the banner the way the app does: a 409 out of the create flow. The
   *  error is the REAL class — the host's triage narrows on `instanceof`. */
  async function raiseBanner(user: ReturnType<typeof userEvent.setup>) {
    let onError: ((e: ApiRequestError) => void) | undefined
    vi.mocked(hooks.useCreateSession).mockReturnValue({
      mutate: vi.fn((_i: unknown, cbs?: { onError?: (e: ApiRequestError) => void }) => {
        onError = cbs?.onError
      }),
      isPending: false,
    } as unknown as ReturnType<typeof hooks.useCreateSession>)

    const app = renderApp({ path: '/sessions?dialog=new', mode: 'desktop' })
    await user.click(await screen.findByRole('button', { name: /^create$/i }))
    act(() => onError?.(new ApiRequestError(409, { error: { code: 'all_accounts_limited' }, earliestReset: null })))
    return app
  }

  it('shows once every account is at its limit', async () => {
    const user = userEvent.setup()
    await raiseBanner(user)
    expect(screen.getByRole('alert')).toHaveTextContent(/all accounts at limit/i)
  })

  it('clears when the user opens the New Session dialog — but not before', async () => {
    const user = userEvent.setup()
    const app = await raiseBanner(user)
    // The 409 that raised it arrived while the dialog was still open, so
    // "dialog open ⇒ no banner" must NOT be a standing rule.
    expect(screen.getByRole('alert')).toBeInTheDocument()

    // Close it (the host does this itself on a 409; here we assert the URL edge).
    await act(async () => { await app.router.navigate('/sessions') })
    expect(screen.getByRole('alert')).toBeInTheDocument()

    // Re-opening is the user acting on the banner: it stops shouting.
    await act(async () => { await app.router.navigate('/sessions?dialog=new') })
    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument())
  })

  it('auto-clears once an account becomes selectable again', async () => {
    const user = userEvent.setup()
    const app = await raiseBanner(user)
    expect(screen.getByRole('alert')).toBeInTheDocument()

    // A non-null rank means the ranker would pick this account: the banner is
    // stale. The shell clears it at render time, so any re-render suffices — a
    // location tick is how the app itself re-renders (see KeyboardSessions).
    setAccounts([account({ rank: 1 })])
    await act(async () => { await app.router.navigate('/sessions?tick=1') })

    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument())
  })
})

/**
 * The shell must hand `useSessionListState` the sessions query's loading flag.
 *
 * Without it the hook treats the first render — empty sessions, so an empty
 * project domain — as authoritative, sanitizes every stored project filter away
 * and writes the emptied value back. The user's filter would vanish on every
 * page load, silently, indistinguishably from "it was never saved".
 */
describe('the shell passes the sessions-loading flag to the list state', () => {
  const STORED = { filters: { status: null, project: 'acme/api', environment: null } }
  const readFilters = (): unknown =>
    (JSON.parse(localStorage.getItem('ccswitch.sessionOrder') ?? '{}') as { filters?: unknown })
      .filters

  it('a stored project filter is NOT sanitized away while the sessions query is loading', async () => {
    localStorage.setItem('ccswitch.sessionOrder', JSON.stringify(STORED))
    vi.mocked(hooks.useSessions).mockReturnValue(loadingQueryResult<Session[]>())
    renderApp({ path: '/sessions', mode: 'desktop' })
    await act(async () => { await Promise.resolve() })

    expect(readFilters()).toEqual(STORED.filters)
  })

  it('…and IS sanitized away once that query has resolved with no sessions', async () => {
    // The discriminator: same empty list, same empty domain — only the flag
    // differs. Drop the flag on the way to the hook and the case above lands
    // here instead, which is exactly the regression.
    localStorage.setItem('ccswitch.sessionOrder', JSON.stringify(STORED))
    setSessions([])
    renderApp({ path: '/sessions', mode: 'desktop' })

    await waitFor(() =>
      expect(readFilters()).toEqual({ status: null, project: null, environment: null }),
    )
  })
})

/**
 * `useDictationField` reads `sttCapability`'s store, not `useConfig()` directly (see
 * `lib/sttCapability.ts`), because it is rendered by Composer/DispatchBar without a
 * QueryClientProvider in many tests. The shell — which already reads config here — is
 * the one place that writes it.
 */
describe('the shell publishes stt capability from config', () => {
  afterEach(() => { setServerSttEnabled(false) })

  it('syncs getServerSttEnabled from config.sttEnabled', async () => {
    vi.mocked(hooks.useConfig).mockReturnValue(
      { data: { repoRoot: '/', adoptionEnabled: false, sttEnabled: true } } as ReturnType<typeof hooks.useConfig>,
    )
    renderApp({ path: '/sessions', mode: 'desktop' })

    await waitFor(() => expect(getServerSttEnabled()).toBe(true))
  })
})
