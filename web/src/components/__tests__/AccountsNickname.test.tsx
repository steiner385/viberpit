import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Dashboard } from '../Dashboard'
import * as hooks from '@/api/hooks'
import type { Account } from '@/api/types'

vi.mock('@/api/hooks', () => ({
  useAccounts: vi.fn(),
  useSessions: vi.fn(),
  useCreateSession: vi.fn(),
  useRenameSession: vi.fn(),
  useCloseSession: vi.fn(),
  useRefreshAccount: vi.fn(),
  useUpdateAccountNickname: vi.fn(),
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

function makeAccount(overrides: Partial<Account> = {}): Account {
  return {
    name: 'work',
    status: 'ok',
    usage: {
      session: { pct: 30, resetsAt: null },
      weekly: { pct: 50, resetsAt: null },
    },
    ...overrides,
  }
}

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

describe('AccountsStrip — nickname display & edit', () => {
  const nicknameMutate = vi.fn()

  beforeEach(() => {
    vi.clearAllMocks()

    vi.mocked(hooks.useSessions).mockReturnValue({
      data: [],
      isLoading: false,
    } as ReturnType<typeof hooks.useSessions>)
    vi.mocked(hooks.useCreateSession).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useCreateSession>)
    vi.mocked(hooks.useRenameSession).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useRenameSession>)
    vi.mocked(hooks.useCloseSession).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useCloseSession>)
    vi.mocked(hooks.useRefreshAccount).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useRefreshAccount>)
    vi.mocked(hooks.useGithubRepos).mockReturnValue({ data: [], isLoading: false, isError: false } as ReturnType<typeof hooks.useGithubRepos>)
    vi.mocked(hooks.useGithubBranches).mockReturnValue({ data: { default: null, branches: [] }, isLoading: false } as ReturnType<typeof hooks.useGithubBranches>)
    vi.mocked(hooks.useSettings).mockReturnValue({ data: { defaultPermissionMode: 'interactive', thinkingCollapsed: false, repoRoot: null }, isLoading: false } as ReturnType<typeof hooks.useSettings>)
    vi.mocked(hooks.useUpdateSettings).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useUpdateSettings>)
    vi.mocked(hooks.useUpdateAccountNickname).mockReturnValue({
      ...NOOP_MUTATION,
      mutate: nicknameMutate,
    } as unknown as ReturnType<typeof hooks.useUpdateAccountNickname>)
    vi.mocked(hooks.useReauthAccount).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useReauthAccount>)
    vi.mocked(hooks.useReauthStatus).mockReturnValue({ data: { state: 'idle', message: null, startedAt: null }, isLoading: false } as ReturnType<typeof hooks.useReauthStatus>)
    vi.mocked(hooks.useCancelReauth).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useCancelReauth>)
  })

  it('shows account name when no nickname is set', () => {
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({ name: 'work', nickname: null })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)

    render(<Dashboard />, { wrapper })

    expect(screen.getByText('work')).toBeInTheDocument()
  })

  it('shows nickname as primary label when set, with name as sub-label', () => {
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({ name: 'work', nickname: 'My Work Account' })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)

    render(<Dashboard />, { wrapper })

    expect(screen.getByText('My Work Account')).toBeInTheDocument()
    // The real name should still appear as sub-label
    expect(screen.getByText('work')).toBeInTheDocument()
  })

  it('shows pencil edit button with aria-label "Rename {name}"', () => {
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({ name: 'work' })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)

    render(<Dashboard />, { wrapper })

    expect(screen.getByRole('button', { name: /rename work/i })).toBeInTheDocument()
  })

  it('clicking the edit button shows an input prefilled with current nickname', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({ name: 'work', nickname: 'Work' })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)

    render(<Dashboard />, { wrapper })

    await user.click(screen.getByRole('button', { name: /rename work/i }))

    const input = screen.getByRole('textbox', { name: /nickname for work/i })
    expect(input).toBeInTheDocument()
    expect((input as HTMLInputElement).value).toBe('Work')
  })

  it('saving the nickname calls the mutation with name and trimmed value', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({ name: 'work', nickname: null })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)

    render(<Dashboard />, { wrapper })

    await user.click(screen.getByRole('button', { name: /rename work/i }))
    const input = screen.getByRole('textbox', { name: /nickname for work/i })
    await user.clear(input)
    await user.type(input, 'Personal')
    await user.click(screen.getByRole('button', { name: /save nickname/i }))

    expect(nicknameMutate).toHaveBeenCalledWith({ name: 'work', nickname: 'Personal' })
  })

  it('pressing Enter in the input saves the nickname', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({ name: 'work', nickname: null })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)

    render(<Dashboard />, { wrapper })

    await user.click(screen.getByRole('button', { name: /rename work/i }))
    const input = screen.getByRole('textbox', { name: /nickname for work/i })
    await user.clear(input)
    await user.type(input, 'Freelance{Enter}')

    expect(nicknameMutate).toHaveBeenCalledWith({ name: 'work', nickname: 'Freelance' })
  })

  it('pressing Escape cancels edit without calling mutation', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({ name: 'work', nickname: null })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)

    render(<Dashboard />, { wrapper })

    await user.click(screen.getByRole('button', { name: /rename work/i }))
    const input = screen.getByRole('textbox', { name: /nickname for work/i })
    await user.type(input, 'Something{Escape}')

    expect(nicknameMutate).not.toHaveBeenCalled()
    // Edit mode should be gone
    await waitFor(() => {
      expect(screen.queryByRole('textbox', { name: /nickname for work/i })).not.toBeInTheDocument()
    })
  })

  it('saving with empty value passes null to mutation (clears nickname)', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({ name: 'work', nickname: 'Old Name' })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)

    render(<Dashboard />, { wrapper })

    await user.click(screen.getByRole('button', { name: /rename work/i }))
    const input = screen.getByRole('textbox', { name: /nickname for work/i })
    await user.clear(input)
    await user.click(screen.getByRole('button', { name: /save nickname/i }))

    expect(nicknameMutate).toHaveBeenCalledWith({ name: 'work', nickname: null })
  })
})

describe('AccountRow — null usage guard (needs-reauth accounts)', () => {
  // Regression: before the fix, an account with usage:null (e.g. needs-reauth) crashed
  // the whole app with "Cannot read properties of null (reading 'session')".

  beforeEach(() => {
    vi.clearAllMocks()

    vi.mocked(hooks.useSessions).mockReturnValue({
      data: [],
      isLoading: false,
    } as ReturnType<typeof hooks.useSessions>)
    vi.mocked(hooks.useCreateSession).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useCreateSession>)
    vi.mocked(hooks.useRenameSession).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useRenameSession>)
    vi.mocked(hooks.useCloseSession).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useCloseSession>)
    vi.mocked(hooks.useRefreshAccount).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useRefreshAccount>)
    vi.mocked(hooks.useGithubRepos).mockReturnValue({ data: [], isLoading: false, isError: false } as ReturnType<typeof hooks.useGithubRepos>)
    vi.mocked(hooks.useGithubBranches).mockReturnValue({ data: { default: null, branches: [] }, isLoading: false } as ReturnType<typeof hooks.useGithubBranches>)
    vi.mocked(hooks.useSettings).mockReturnValue({ data: { defaultPermissionMode: 'interactive', thinkingCollapsed: false, repoRoot: null }, isLoading: false } as ReturnType<typeof hooks.useSettings>)
    vi.mocked(hooks.useUpdateSettings).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useUpdateSettings>)
    vi.mocked(hooks.useUpdateAccountNickname).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useUpdateAccountNickname>)
    vi.mocked(hooks.useReauthAccount).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useReauthAccount>)
    vi.mocked(hooks.useReauthStatus).mockReturnValue({ data: { state: 'idle', message: null, startedAt: null }, isLoading: false } as ReturnType<typeof hooks.useReauthStatus>)
    vi.mocked(hooks.useCancelReauth).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useCancelReauth>)
  })

  it('renders without throwing when account.usage is null (needs-reauth)', () => {
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({ name: 'kindash', status: 'needs-relogin', usage: null, nickname: null })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)

    // Should not throw — previously crashed with "Cannot read properties of null"
    expect(() => render(<Dashboard />, { wrapper })).not.toThrow()
  })

  it('does not render any UsageBadge when account.usage is null', () => {
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({ name: 'kindash', status: 'needs-relogin', usage: null, nickname: null })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)

    render(<Dashboard />, { wrapper })

    // UsageBadge renders with aria-label "session usage: N%" / "weekly usage: N%".
    // Neither should be present when usage is null.
    expect(screen.queryByLabelText(/session usage:/i)).not.toBeInTheDocument()
    expect(screen.queryByLabelText(/weekly usage:/i)).not.toBeInTheDocument()
  })

  it('still renders the account name when usage is null', () => {
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({ name: 'kindash', status: 'needs-relogin', usage: null, nickname: null })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)

    render(<Dashboard />, { wrapper })

    expect(screen.getByText('kindash')).toBeInTheDocument()
  })
})

describe('NewSessionDialog — account select uses nickname', () => {
  beforeEach(() => {
    vi.clearAllMocks()

    vi.mocked(hooks.useSessions).mockReturnValue({ data: [], isLoading: false } as ReturnType<typeof hooks.useSessions>)
    vi.mocked(hooks.useCreateSession).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useCreateSession>)
    vi.mocked(hooks.useRenameSession).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useRenameSession>)
    vi.mocked(hooks.useCloseSession).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useCloseSession>)
    vi.mocked(hooks.useRefreshAccount).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useRefreshAccount>)
    vi.mocked(hooks.useUpdateAccountNickname).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useUpdateAccountNickname>)
    vi.mocked(hooks.useGithubRepos).mockReturnValue({ data: [], isLoading: false, isError: false } as ReturnType<typeof hooks.useGithubRepos>)
    vi.mocked(hooks.useGithubBranches).mockReturnValue({ data: { default: null, branches: [] }, isLoading: false } as ReturnType<typeof hooks.useGithubBranches>)
    vi.mocked(hooks.useSettings).mockReturnValue({ data: { defaultPermissionMode: 'interactive', thinkingCollapsed: false, repoRoot: null }, isLoading: false } as ReturnType<typeof hooks.useSettings>)
    vi.mocked(hooks.useUpdateSettings).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useUpdateSettings>)
    vi.mocked(hooks.useReauthAccount).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useReauthAccount>)
    vi.mocked(hooks.useReauthStatus).mockReturnValue({ data: { state: 'idle', message: null, startedAt: null }, isLoading: false } as ReturnType<typeof hooks.useReauthStatus>)
    vi.mocked(hooks.useCancelReauth).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useCancelReauth>)
  })

  it('shows nickname as option label in account select when set', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [
        makeAccount({ name: 'work', nickname: 'Work Account' }),
        makeAccount({ name: 'personal', nickname: null }),
      ],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)

    render(<Dashboard />, { wrapper })

    // Open new session dialog
    await user.click(screen.getAllByRole('button', { name: /new session/i })[0])

    // Use the select's id directly to avoid ambiguity with the "Accounts" section heading
    const accountSelect = document.getElementById('new-session-account') as HTMLSelectElement
    expect(accountSelect).toBeTruthy()
    const options = Array.from(accountSelect.options).map((o) => ({ value: o.value, label: o.text }))

    const workOption = options.find((o) => o.value === 'work')
    expect(workOption?.label).toBe('Work Account')

    const personalOption = options.find((o) => o.value === 'personal')
    expect(personalOption?.label).toBe('personal')
  })

  it('option value stays the real account name even when nickname is shown', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({ name: 'work', nickname: 'My Work' })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)

    render(<Dashboard />, { wrapper })

    await user.click(screen.getAllByRole('button', { name: /new session/i })[0])

    const accountSelect = document.getElementById('new-session-account') as HTMLSelectElement
    expect(accountSelect).toBeTruthy()
    const workOpt = Array.from(accountSelect.options).find((o) => o.value === 'work')
    expect(workOpt).toBeTruthy()
    expect(workOpt?.text).toBe('My Work')
  })
})
