/**
 * `NewSessionDialogHost` — the `?dialog=new` reader and the create-session flow.
 *
 * ## What lives here, and where it came from
 *
 * The flow was `Dashboard.handleCreateSubmit`, and its tests were spread across
 * three files that all mounted a shell to reach it:
 *
 *   • `DashboardSettings.test.tsx` — the Tool-approvals default (4 cases)
 *   • `DashboardAllLimited.test.tsx` — the optimistic toast and the 409 triage (3)
 *   • `DesktopShell.test.tsx` — the SAME three cases again, against the other
 *     shell (3). Those three are the "true duplicates" in Task 18's count delta:
 *     one flow, tested twice because there were two shells.
 *
 * All of it collapses here, onto the component that owns the flow. The
 * `?dialog=new` URL contract itself is `App.dialogs.test.tsx`'s.
 *
 * ## The 409 triage is the point
 *
 * Three different 409 bodies mean three different things, and the shells had
 * already drifted on them (`Dashboard` swallowed "all accounts limited" where
 * `DesktopShell` raised the banner). Each branch gets a case, including
 * `branch_switch_failed` — the one that must keep the dialog OPEN, which nothing
 * previously asserted.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { toast } from 'sonner'
import * as hooks from '@/api/hooks'
import { ApiRequestError } from '@/api/client'
import type { Account, Session } from '@/api/types'
import { session as makeSession } from '@/test/factories'
import { NewSessionDialogHost } from './NewSessionDialogHost'
import { CREATE_SESSION_FALLBACK } from './createSessionFailure'

vi.mock('sonner', () => ({
  toast: Object.assign(vi.fn(), { error: vi.fn(), success: vi.fn() }),
}))

vi.mock('@/api/hooks', () => ({
  useCreateSession: vi.fn(),
  useSettings: vi.fn(),
  useModels: vi.fn(() => ({ data: [], isLoading: false })),
  useGithubRepos: vi.fn(() => ({ data: [], isLoading: false, isError: false })),
  useGithubBranches: vi.fn(() => ({ data: { default: null, branches: [] }, isLoading: false })),
}))

/** A real `Session`, not a cast (R10) — shape from @/test/factories, with the
 *  just-created defaults this suite works with. */
const session = (over: Partial<Session> = {}): Session =>
  makeSession({ id: 'new-1', name: 'New', status: 'in_progress', ...over })

const ACCOUNTS: Account[] = [
  {
    name: 'alice', status: 'ok',
    usage: { session: { pct: 1, resetsAt: null }, weekly: { pct: 1, resetsAt: null } },
    liveSessions: { active: 0, idle: 0 }, rank: 1, rankReason: null,
  },
]

/** The `mutate` the host calls, with the callbacks it was handed. `onError`
 *  takes the REAL error class: the triage narrows on `instanceof`, and a
 *  plain `{status, body}` object would let a broken narrowing pass. */
type CreateCallbacks = {
  onSuccess?: (s: Session) => void
  onError?: (e: ApiRequestError) => void
}
let lastCallbacks: CreateCallbacks = {}
let createMutate: ReturnType<typeof vi.fn>

function Location() {
  const { pathname, state } = useLocation()
  return <div data-testid="url">{pathname}|{JSON.stringify(state)}</div>
}

function renderHost({ onAllLimited = vi.fn() } = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/sessions?dialog=new']}>
        <NewSessionDialogHost accounts={ACCOUNTS} onAllLimited={onAllLimited} />
        <Location />
      </MemoryRouter>
    </QueryClientProvider>,
  )
  return { onAllLimited }
}

const url = () => screen.getByTestId('url').textContent ?? ''
const dialogOpen = () => screen.queryByRole('dialog', { name: /new session/i }) !== null

beforeEach(() => {
  vi.clearAllMocks()
  lastCallbacks = {}
  createMutate = vi.fn((_input: unknown, cbs?: CreateCallbacks) => { lastCallbacks = cbs ?? {} })
  vi.mocked(hooks.useCreateSession).mockReturnValue(
    { mutate: createMutate, isPending: false } as unknown as ReturnType<typeof hooks.useCreateSession>,
  )
  vi.mocked(hooks.useSettings).mockReturnValue(
    { data: { defaultPermissionMode: 'auto', thinkingCollapsed: false, repoRoot: null }, isLoading: false } as ReturnType<typeof hooks.useSettings>,
  )
})

describe('NewSessionDialogHost — the Tool approvals default', () => {
  it('shows a Tool approvals select in the New Session dialog', () => {
    renderHost()
    expect(screen.getByLabelText(/tool approvals/i)).toBeInTheDocument()
  })

  it('defaults the Tool approvals select to the settings default', () => {
    renderHost()
    expect((screen.getByLabelText(/tool approvals/i) as HTMLSelectElement).value).toBe('auto')
  })

  it('falls back to "interactive" when settings data is not yet loaded', () => {
    vi.mocked(hooks.useSettings).mockReturnValue(
      { data: undefined, isLoading: true } as ReturnType<typeof hooks.useSettings>,
    )
    renderHost()
    expect((screen.getByLabelText(/tool approvals/i) as HTMLSelectElement).value).toBe('interactive')
  })

  it('submits permissionMode in the create session payload', async () => {
    const user = userEvent.setup()
    renderHost()
    await user.selectOptions(screen.getByLabelText(/tool approvals/i), 'full-auto')
    await user.click(screen.getByRole('button', { name: /^create$/i }))
    expect(createMutate).toHaveBeenCalledWith(
      expect.objectContaining({ permissionMode: 'full-auto' }),
      expect.anything(),
    )
  })
})

describe('NewSessionDialogHost — success', () => {
  it('opens the new session, carrying the prompt in router state', async () => {
    const user = userEvent.setup()
    renderHost()
    await user.type(screen.getByLabelText(/initial prompt/i), 'fix the bug')
    await user.click(screen.getByRole('button', { name: /^create$/i }))
    act(() => lastCallbacks.onSuccess?.(session({ id: 'new-1' })))

    expect(url()).toContain('/sessions/new-1')
    expect(url()).toContain('"initialPrompt":"fix the bug"')
    expect(dialogOpen()).toBe(false)
  })

  it('shows a subtle toast when the session started without a verified quota', async () => {
    const user = userEvent.setup()
    renderHost()
    await user.click(screen.getByRole('button', { name: /^create$/i }))
    act(() => lastCallbacks.onSuccess?.(session({ usageUnverified: true })))

    expect(vi.mocked(toast)).toHaveBeenCalledWith(expect.stringContaining('started anyway'))
  })

  it('stays quiet when the quota WAS verified', async () => {
    const user = userEvent.setup()
    renderHost()
    await user.click(screen.getByRole('button', { name: /^create$/i }))
    act(() => lastCallbacks.onSuccess?.(session({ usageUnverified: false })))

    expect(vi.mocked(toast)).not.toHaveBeenCalled()
  })
})

describe('NewSessionDialogHost — the 409 triage', () => {
  async function submitThenFail(status: number, body: Record<string, unknown>, onAllLimited = vi.fn()) {
    const user = userEvent.setup()
    renderHost({ onAllLimited })
    await user.click(screen.getByRole('button', { name: /^create$/i }))
    act(() => lastCallbacks.onError?.(new ApiRequestError(status, body)))
    return { onAllLimited }
  }
  const submitThen409 = (body: Record<string, unknown>, onAllLimited?: Parameters<typeof submitThenFail>[2]) =>
    submitThenFail(409, body, onAllLimited)

  it('keeps the dialog OPEN for accounts_need_reauth and says so inline — no toast, no limit banner', async () => {
    const { onAllLimited } = await submitThen409({ error: { code: 'accounts_need_reauth' } })
    expect(screen.getByRole('alert')).toHaveTextContent(/re-authenticate/)
    expect(vi.mocked(toast.error)).not.toHaveBeenCalled()
    expect(onAllLimited).not.toHaveBeenCalled()
    expect(dialogOpen()).toBe(true)
  })

  it('keeps the dialog OPEN for a non-409 failure and shows the message inline', async () => {
    // `useCreateSession` opts out of the global mutation toast, so this line is
    // the ONLY thing the user would see — it must be there.
    await submitThenFail(500, {})
    expect(screen.getByRole('alert')).toHaveTextContent(CREATE_SESSION_FALLBACK)
    expect(vi.mocked(toast.error)).not.toHaveBeenCalled()
    expect(dialogOpen()).toBe(true)
  })

  it('hands an all-accounts-limited reset time to the shell banner', async () => {
    const { onAllLimited } = await submitThen409({
      error: { code: 'all_accounts_limited' },
      earliestReset: '2026-06-19T12:00:00Z',
    })
    expect(onAllLimited).toHaveBeenCalledWith('2026-06-19T12:00:00Z')
    expect(vi.mocked(toast.error)).not.toHaveBeenCalled()
    expect(dialogOpen()).toBe(false)
  })

  it('passes null when the daemon could not say when capacity returns', async () => {
    const { onAllLimited } = await submitThen409({ error: { code: 'all_accounts_limited' } })
    expect(onAllLimited).toHaveBeenCalledWith(null)
  })

  it('keeps the dialog OPEN for a branch failure, and shows the message inline', async () => {
    await submitThen409({
      error: { code: 'branch_switch_failed', message: 'Could not switch to feat/x' },
    })
    // The user picked a bad branch and can pick another — closing the form would
    // throw away everything else they had typed.
    expect(dialogOpen()).toBe(true)
    expect(screen.getByRole('alert')).toHaveTextContent('Could not switch to feat/x')
  })

  it('never raises the limit banner for a non-409 error', async () => {
    const { onAllLimited } = await submitThenFail(500, { error: { code: 'boom' } })
    expect(onAllLimited).not.toHaveBeenCalled()
  })
})
