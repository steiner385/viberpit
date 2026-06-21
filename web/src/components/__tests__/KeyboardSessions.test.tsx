/**
 * KeyboardSessions.test.tsx
 *
 * Integration tests for Tasks 3–6: selection state, row actions,
 * help overlay, and dispatch wired into Dashboard.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Dashboard } from '../Dashboard'
import * as hooks from '@/api/hooks'
import type { Account, Session } from '@/api/types'

// ── Mock @/api/hooks (same scaffold as DashboardTabs.test.tsx) ────────────────

vi.mock('@/api/hooks', () => ({
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
  QK: {
    accounts: ['accounts'],
    sessions: ['sessions'],
    config: ['config'],
    githubRepos: ['github-repos'],
    githubBranches: (repo?: string) => ['github-branches', repo],
    settings: ['settings'],
  },
}))

const NOOP_MUTATION = { mutate: vi.fn(), isPending: false, isSuccess: false }

function makeSession(overrides: Partial<Session> = {}): Session {
  return {
    id: 'sess-1',
    name: 'My Session',
    accountName: 'work',
    status: 'idle',
    createdAt: new Date().toISOString(),
    updatedAt: new Date().toISOString(),
    ...overrides,
  }
}

const THREE: Session[] = [
  makeSession({ id: 's1', name: 'One' }),
  makeSession({ id: 's2', name: 'Two' }),
  makeSession({ id: 's3', name: 'Three' }),
]

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

function setupDefaultMocks() {
  vi.mocked(hooks.useAccounts).mockReturnValue({ data: [], isLoading: false } as ReturnType<typeof hooks.useAccounts>)
  vi.mocked(hooks.useSessions).mockReturnValue({ data: [], isLoading: false } as ReturnType<typeof hooks.useSessions>)
  vi.mocked(hooks.useCreateSession).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useCreateSession>)
  vi.mocked(hooks.useRenameSession).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useRenameSession>)
  vi.mocked(hooks.useCloseSession).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useCloseSession>)
  vi.mocked(hooks.useRefreshAccount).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useRefreshAccount>)
  vi.mocked(hooks.useUpdateAccountNickname).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useUpdateAccountNickname>)
  vi.mocked(hooks.useDeleteAccount).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useDeleteAccount>)
  vi.mocked(hooks.useGithubRepos).mockReturnValue({ data: [], isLoading: false, isError: false } as ReturnType<typeof hooks.useGithubRepos>)
  vi.mocked(hooks.useGithubBranches).mockReturnValue({ data: { default: null, branches: [] }, isLoading: false } as ReturnType<typeof hooks.useGithubBranches>)
  vi.mocked(hooks.useSettings).mockReturnValue({ data: { defaultPermissionMode: 'interactive', thinkingCollapsed: false, repoRoot: null }, isLoading: false } as ReturnType<typeof hooks.useSettings>)
  vi.mocked(hooks.useUpdateSettings).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useUpdateSettings>)
  vi.mocked(hooks.useReauthAccount).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useReauthAccount>)
  vi.mocked(hooks.useReauthStatus).mockReturnValue({ data: { state: 'idle', message: null, startedAt: null }, isLoading: false } as ReturnType<typeof hooks.useReauthStatus>)
  vi.mocked(hooks.useCancelReauth).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useCancelReauth>)
}

// ── Task 3: Selection state + nav ─────────────────────────────────────────────

describe('Task 3 — session-list selection + keyboard nav', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    setupDefaultMocks()
    window.localStorage.clear()
  })

  it('ArrowDown highlights the first session, ArrowDown again the second', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useSessions).mockReturnValue({ data: THREE, isLoading: false } as any)
    render(<Dashboard />, { wrapper })
    const box = screen.getByRole('textbox', { name: /new session prompt/i })
    await user.click(box)
    await user.keyboard('{ArrowDown}')
    expect(screen.getByLabelText(/open session one/i)).toHaveAttribute('data-selected', 'true')
    await user.keyboard('{ArrowDown}')
    expect(screen.getByLabelText(/open session two/i)).toHaveAttribute('data-selected', 'true')
  })

  it('ArrowUp clamps at index 0 (does not go to -1)', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useSessions).mockReturnValue({ data: THREE, isLoading: false } as any)
    render(<Dashboard />, { wrapper })
    await user.click(screen.getByRole('textbox', { name: /new session prompt/i }))
    await user.keyboard('{ArrowDown}') // index 0
    await user.keyboard('{ArrowUp}')  // should stay at 0
    expect(screen.getByLabelText(/open session one/i)).toHaveAttribute('data-selected', 'true')
  })

  it('Enter on empty prompt opens the highlighted session', async () => {
    const user = userEvent.setup()
    const onOpen = vi.fn()
    vi.mocked(hooks.useSessions).mockReturnValue({ data: THREE, isLoading: false } as any)
    render(<Dashboard onOpenSession={onOpen} />, { wrapper })
    await user.click(screen.getByRole('textbox', { name: /new session prompt/i }))
    await user.keyboard('{ArrowDown}{Enter}')
    expect(onOpen).toHaveBeenCalledWith('s1')
  })

  it('Escape clears the highlight (selectedIndex → -1)', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useSessions).mockReturnValue({ data: THREE, isLoading: false } as any)
    render(<Dashboard />, { wrapper })
    await user.click(screen.getByRole('textbox', { name: /new session prompt/i }))
    await user.keyboard('{ArrowDown}') // index 0 selected
    await user.keyboard('{Escape}')
    // after escape, no session row should be data-selected=true
    for (const sess of THREE) {
      const btn = screen.getByLabelText(`Open session ${sess.name}`)
      expect(btn).not.toHaveAttribute('data-selected', 'true')
    }
  })

  it('dispatch textarea has aria-activedescendant pointing at the selected row', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useSessions).mockReturnValue({ data: THREE, isLoading: false } as any)
    render(<Dashboard />, { wrapper })
    const box = screen.getByRole('textbox', { name: /new session prompt/i })
    await user.click(box)
    await user.keyboard('{ArrowDown}')
    expect(box).toHaveAttribute('aria-activedescendant', 'row-s1')
  })

  it('nav is a no-op when the session list is empty', async () => {
    const user = userEvent.setup()
    const onOpen = vi.fn()
    vi.mocked(hooks.useSessions).mockReturnValue({ data: [], isLoading: false } as any)
    render(<Dashboard onOpenSession={onOpen} />, { wrapper })
    await user.click(screen.getByRole('textbox', { name: /new session prompt/i }))
    await user.keyboard('{ArrowDown}{Enter}')
    expect(onOpen).not.toHaveBeenCalled()
  })
})

// ── Task 4: Row actions ───────────────────────────────────────────────────────

describe('Task 4 — row actions on highlighted session', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    setupDefaultMocks()
    window.localStorage.clear()
  })

  it('r on empty prompt opens the rename dialog for the highlighted session', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useSessions).mockReturnValue({ data: THREE, isLoading: false } as any)
    render(<Dashboard />, { wrapper })
    await user.click(screen.getByRole('textbox', { name: /new session prompt/i }))
    await user.keyboard('{ArrowDown}r')
    expect(screen.getByRole('dialog', { name: /rename session/i })).toBeInTheDocument()
  })

  it('Delete on empty prompt calls the close hook for the highlighted session', async () => {
    const user = userEvent.setup()
    const closeMutate = vi.fn()
    vi.mocked(hooks.useCloseSession).mockReturnValue({ mutate: closeMutate, isPending: false } as any)
    vi.mocked(hooks.useSessions).mockReturnValue({ data: THREE, isLoading: false } as any)
    render(<Dashboard />, { wrapper })
    await user.click(screen.getByRole('textbox', { name: /new session prompt/i }))
    await user.keyboard('{ArrowDown}{Delete}')
    expect(closeMutate).toHaveBeenCalledWith('s1')
  })

  it('Alt+3 opens the third session directly', async () => {
    const user = userEvent.setup()
    const onOpen = vi.fn()
    vi.mocked(hooks.useSessions).mockReturnValue({ data: THREE, isLoading: false } as any)
    render(<Dashboard onOpenSession={onOpen} />, { wrapper })
    await user.click(screen.getByRole('textbox', { name: /new session prompt/i }))
    await user.keyboard('{Alt>}3{/Alt}')
    expect(onOpen).toHaveBeenCalledWith('s3')
  })

  it('Alt+9 is a no-op when fewer than 9 sessions', async () => {
    const user = userEvent.setup()
    const onOpen = vi.fn()
    vi.mocked(hooks.useSessions).mockReturnValue({ data: THREE, isLoading: false } as any)
    render(<Dashboard onOpenSession={onOpen} />, { wrapper })
    await user.click(screen.getByRole('textbox', { name: /new session prompt/i }))
    await user.keyboard('{Alt>}9{/Alt}')
    expect(onOpen).not.toHaveBeenCalled()
  })

  it('selectedIndex clamps when sessions list shrinks', async () => {
    const user = userEvent.setup()
    const { rerender } = render(<Dashboard />, { wrapper })
    // Start with 3 sessions, select last one (index 2)
    vi.mocked(hooks.useSessions).mockReturnValue({ data: THREE, isLoading: false } as any)
    rerender(<Dashboard />)
    await user.click(screen.getByRole('textbox', { name: /new session prompt/i }))
    await user.keyboard('{ArrowDown}{ArrowDown}{ArrowDown}')
    expect(screen.getByLabelText(/open session three/i)).toHaveAttribute('data-selected', 'true')

    // Now simulate list shrinking to 2 sessions
    const TWO = THREE.slice(0, 2)
    vi.mocked(hooks.useSessions).mockReturnValue({ data: TWO, isLoading: false } as any)
    rerender(<Dashboard />)

    // After clamp, index should be 1 (last item in TWO)
    await waitFor(() => {
      expect(screen.getByLabelText(/open session two/i)).toHaveAttribute('data-selected', 'true')
    })
  })
})

// ── Task 5: ShortcutsHelp overlay (wired into Dashboard) ─────────────────────

describe('Task 5 — ? shortcuts help overlay in Dashboard', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    setupDefaultMocks()
    window.localStorage.clear()
  })

  it('? opens the shortcuts help dialog', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useSessions).mockReturnValue({ data: [], isLoading: false } as any)
    render(<Dashboard />, { wrapper })
    await user.click(screen.getByRole('textbox', { name: /new session prompt/i }))
    await user.keyboard('{Shift>}/{/Shift}')
    expect(screen.getByRole('dialog', { name: /keyboard shortcuts/i })).toBeInTheDocument()
  })

  it('Esc closes the shortcuts help dialog', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useSessions).mockReturnValue({ data: [], isLoading: false } as any)
    render(<Dashboard />, { wrapper })
    await user.click(screen.getByRole('textbox', { name: /new session prompt/i }))
    await user.keyboard('{Shift>}/{/Shift}')
    expect(screen.getByRole('dialog', { name: /keyboard shortcuts/i })).toBeInTheDocument()
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('dialog', { name: /keyboard shortcuts/i })).not.toBeInTheDocument()
  })
})

// ── Task 6: Dispatch create-and-open + auto-focus ─────────────────────────────

describe('Task 6 — dispatch create-and-open', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    setupDefaultMocks()
    window.localStorage.clear()
  })

  it('Enter with text creates a session with the prompt + repo default branch and opens it', async () => {
    const user = userEvent.setup()
    const createMutate = vi.fn((_input: unknown, opts: any) => opts?.onSuccess?.({ id: 'new-1' }))
    const onOpen = vi.fn()
    vi.mocked(hooks.useCreateSession).mockReturnValue({ mutate: createMutate, isPending: false } as any)
    vi.mocked(hooks.useGithubRepos).mockReturnValue({
      data: [{ nameWithOwner: 'acme/web', name: 'web', owner: 'acme', isPrivate: false, url: '', defaultBranch: 'main', pushedAt: null, cloned: true }],
      isLoading: false, isError: false,
    } as any)
    window.localStorage.setItem('ccswitch.lastRepo', 'acme/web')
    render(<Dashboard onOpenSession={onOpen} />, { wrapper })
    const box = screen.getByRole('textbox', { name: /new session prompt/i })
    await user.click(box)
    await user.type(box, 'add a button')
    await user.keyboard('{Enter}')
    expect(createMutate).toHaveBeenCalledWith(
      { initialPrompt: 'add a button', repo: 'acme/web', branch: 'main' },
      expect.anything(),
    )
    expect(onOpen).toHaveBeenCalledWith('new-1')
  })

  it('dispatch with no repo selected omits repo and branch', async () => {
    const user = userEvent.setup()
    const createMutate = vi.fn((_i: unknown, opts: any) => opts?.onSuccess?.({ id: 'n2' }))
    vi.mocked(hooks.useCreateSession).mockReturnValue({ mutate: createMutate, isPending: false } as any)
    render(<Dashboard onOpenSession={vi.fn()} />, { wrapper })
    const box = screen.getByRole('textbox', { name: /new session prompt/i })
    await user.click(box)
    await user.type(box, 'hello')
    await user.keyboard('{Enter}')
    expect(createMutate).toHaveBeenCalledWith(
      { initialPrompt: 'hello', repo: undefined, branch: undefined },
      expect.anything(),
    )
  })
})
