import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { DesktopShell } from '../DesktopShell'
import * as hooks from '@/api/hooks'
import type { Session } from '@/api/types'

vi.mock('@/ws/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/ws/client')>()
  return {
    ...actual,
    useSessionSocket: () => ({
      attach: vi.fn(), detach: vi.fn(), interrupt: vi.fn(), prompt: vi.fn(),
      sendInterrupt: vi.fn(), permissionResponse: vi.fn(), answerQuestion: vi.fn(),
      setPermissionMode: vi.fn(), cancelQueued: vi.fn(), on: vi.fn(), off: vi.fn(),
      connectionState: 'connected' as const, socket: null,
    }),
  }
})

vi.mock('@/api/hooks', () => ({
  useAccounts: vi.fn(), useSessions: vi.fn(), useCreateSession: vi.fn(),
  useRenameSession: vi.fn(), useCloseSession: vi.fn(), useRefreshAccount: vi.fn(),
  useUpdateAccountNickname: vi.fn(), useDeleteAccount: vi.fn(), useGithubRepos: vi.fn(),
  useGithubBranches: vi.fn(), useSettings: vi.fn(), useUpdateSettings: vi.fn(),
  useReauthAccount: vi.fn(), useReauthStatus: vi.fn(), useCancelReauth: vi.fn(),
  useArchivedSessions: vi.fn(), useUnarchiveSession: vi.fn(), usePermanentDeleteSession: vi.fn(),
  useModels: vi.fn(), useSendPrompt: vi.fn(),
  QK: { accounts: ['accounts'], sessions: ['sessions'], archived: ['archived-sessions'], config: ['config'], githubRepos: ['github-repos'], githubBranches: (r?: string) => ['github-branches', r], settings: ['settings'] },
}))

vi.mock('@/session/useSessionSearch', () => ({
  useSessionSearch: () => ({ query: '', setQuery: vi.fn(), matchedIds: null, isFiltered: false }),
}))

const NOOP = { mutate: vi.fn(), isPending: false, isSuccess: false }
function mk(o: Partial<Session> = {}): Session {
  return { id: 's1', name: 'One', accountName: 'work', status: 'idle', createdAt: '', updatedAt: '', ...o }
}
function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}
function setup() {
  vi.mocked(hooks.useAccounts).mockReturnValue({ data: [], isLoading: false } as ReturnType<typeof hooks.useAccounts>)
  vi.mocked(hooks.useSessions).mockReturnValue({ data: [mk()], isLoading: false } as ReturnType<typeof hooks.useSessions>)
  vi.mocked(hooks.useModels).mockReturnValue({ data: [], isLoading: false } as ReturnType<typeof hooks.useModels>)
  for (const h of ['useCreateSession','useRenameSession','useCloseSession','useRefreshAccount','useUpdateAccountNickname','useDeleteAccount','useUpdateSettings','useReauthAccount','useCancelReauth','useUnarchiveSession','usePermanentDeleteSession','useSendPrompt'] as const) {
    vi.mocked(hooks[h]).mockReturnValue(NOOP as never)
  }
  vi.mocked(hooks.useGithubRepos).mockReturnValue({ data: [], isLoading: false, isError: false } as ReturnType<typeof hooks.useGithubRepos>)
  vi.mocked(hooks.useGithubBranches).mockReturnValue({ data: { default: null, branches: [] }, isLoading: false } as ReturnType<typeof hooks.useGithubBranches>)
  vi.mocked(hooks.useSettings).mockReturnValue({ data: { defaultPermissionMode: 'interactive', thinkingCollapsed: false, repoRoot: null }, isLoading: false } as ReturnType<typeof hooks.useSettings>)
  vi.mocked(hooks.useReauthStatus).mockReturnValue({ data: { state: 'idle', message: null, startedAt: null }, isLoading: false } as ReturnType<typeof hooks.useReauthStatus>)
  vi.mocked(hooks.useArchivedSessions).mockReturnValue({ data: [], isLoading: false } as ReturnType<typeof hooks.useArchivedSessions>)
}

describe('DesktopShell', () => {
  beforeEach(() => { vi.clearAllMocks(); setup(); window.localStorage.clear() })

  it('defaults to the New Session target: shows the new-session prompt and the slot is selected', () => {
    render(<DesktopShell />, { wrapper })
    expect(screen.getByRole('textbox', { name: /new session prompt/i })).toBeInTheDocument()
    expect(screen.getByLabelText(/^new session$/i)).toHaveAttribute('data-selected', 'true')
  })

  it('selecting a session shows its body and switches the prompt to the message composer', async () => {
    const user = userEvent.setup()
    render(<DesktopShell />, { wrapper })
    await user.click(screen.getByLabelText(/open session one/i))
    expect(screen.getByRole('textbox', { name: /^message$/i })).toBeInTheDocument()
    expect(screen.queryByRole('textbox', { name: /new session prompt/i })).not.toBeInTheDocument()
  })

  it('Accounts tab renders the accounts manager in the main pane', async () => {
    const user = userEvent.setup()
    render(<DesktopShell />, { wrapper })
    await user.click(screen.getByRole('tab', { name: /accounts/i }))
    expect(screen.getByRole('heading', { name: /^accounts$/i })).toBeInTheDocument()
  })

  it('deleting a session opens a confirm dialog; the mutation runs only on confirm', async () => {
    const user = userEvent.setup()
    const deleteMutate = vi.fn()
    vi.mocked(hooks.usePermanentDeleteSession).mockReturnValue(
      { mutate: deleteMutate, isPending: false, isSuccess: false } as never,
    )
    render(<DesktopShell />, { wrapper })

    await user.click(screen.getByRole('button', { name: /delete session one/i }))

    // Destructive: the confirm dialog appears and nothing is deleted yet.
    const dialog = screen.getByRole('dialog')
    expect(deleteMutate).not.toHaveBeenCalled()

    await user.click(within(dialog).getByRole('button', { name: /delete permanently/i }))
    expect(deleteMutate).toHaveBeenCalledWith('s1', expect.anything())
  })

  it('ArrowLeft on an empty composer detaches to the welcome pane and highlights the origin row', async () => {
    const user = userEvent.setup()
    render(<DesktopShell />, { wrapper })
    await user.click(screen.getByLabelText(/open session one/i))
    expect(screen.getByRole('textbox', { name: /^message$/i })).toBeInTheDocument()
    await user.keyboard('{ArrowLeft}')
    expect(screen.getByRole('textbox', { name: /new session prompt/i })).toBeInTheDocument()
    expect(screen.getByText(/select a session from the left/i)).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: /new session prompt/i }))
      .toHaveAttribute('aria-activedescendant', 'row-s1')
  })

  it('ArrowDown on the new-session prompt makes the keyboard-highlighted row visibly selected', async () => {
    const user = userEvent.setup()
    render(<DesktopShell />, { wrapper })
    const prompt = screen.getByRole('textbox', { name: /new session prompt/i })
    await user.click(prompt)
    await user.keyboard('{ArrowDown}')
    // Screen-reader path: the textbox points at the highlighted row.
    expect(prompt).toHaveAttribute('aria-activedescendant', 'row-s1')
    // Sighted path: that row must actually render as selected, not just be the
    // ARIA active descendant. This is the regression that made ↑/↓ look dead.
    expect(screen.getByLabelText(/open session one/i)).toHaveAttribute('data-selected', 'true')
  })

  it('Ctrl+Z detaches to the welcome pane', async () => {
    const user = userEvent.setup()
    render(<DesktopShell />, { wrapper })
    await user.click(screen.getByLabelText(/open session one/i))
    await user.keyboard('{Control>}z{/Control}')
    expect(screen.getByRole('textbox', { name: /new session prompt/i })).toBeInTheDocument()
  })

  it('ArrowLeft with composer text does NOT detach', async () => {
    const user = userEvent.setup()
    render(<DesktopShell />, { wrapper })
    await user.click(screen.getByLabelText(/open session one/i))
    const box = screen.getByRole('textbox', { name: /^message$/i })
    await user.click(box)
    await user.type(box, 'wip')
    await user.keyboard('{ArrowLeft}')
    expect(screen.getByRole('textbox', { name: /^message$/i })).toBeInTheDocument()
    expect(screen.queryByRole('textbox', { name: /new session prompt/i })).not.toBeInTheDocument()
  })

  it('DesktopShell: groups by status and keeps keyboard nav', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useSessions).mockReturnValue({
      data: [
        mk({ id: 's1', name: 'One', status: 'idle' }),
        mk({ id: 's2', name: 'Two', status: 'in_progress' }),
      ],
      isLoading: false,
    } as ReturnType<typeof hooks.useSessions>)
    render(<DesktopShell />, { wrapper })
    await user.selectOptions(screen.getByLabelText('Group by'), 'status')
    // Two sessions from DIFFERENT tiers → both group headers must render
    // (idle → 'your-turn' tier, header "Done"; in_progress → 'busy' tier, header "Working")
    expect(screen.getByText('Done')).toBeInTheDocument()
    expect(screen.getByText('Working')).toBeInTheDocument()
    // keyboard nav still works: ArrowDown should highlight a row
    const prompt = screen.getByRole('textbox', { name: /new session prompt/i })
    await user.click(prompt)
    await user.keyboard('{ArrowDown}')
    expect(prompt.getAttribute('aria-activedescendant')).toMatch(/^row-s/)
  })
})
