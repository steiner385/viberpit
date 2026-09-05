/**
 * AccountsManager — Copilot provider chip + reauth tests.
 *
 * Mounts AccountsManager directly (no Dashboard wrapper) so we only need the
 * hooks that AccountsManager + AddAccountDialog actually consume.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { AccountsManager } from '../AccountsManager'
import * as hooks from '@/api/hooks'
import type { Account, ReauthState } from '@/api/types'
import { queryResult } from '@/test/reactQuery'
import { anthropicUsage, copilotUsage } from '@/test/factories'

vi.mock('@/api/hooks', () => ({
  useAccounts: vi.fn(),
  useSessions: vi.fn(),
  useRefreshAccount: vi.fn(),
  useUpdateAccount: vi.fn(),
  useDeleteAccount: vi.fn(),
  useSetAccountEnabled: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useReauthAccount: vi.fn(),
  useReauthStatus: vi.fn(),
  useCancelReauth: vi.fn(),
  useSubmitReauthCode: vi.fn(),
  useOpenAIReauth: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useOpenAIReauthStatus: vi.fn(() => ({ data: undefined, isLoading: false })),
  useCancelOpenAIReauth: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useAddAccount: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useAddAccountStatus: vi.fn(() => ({ data: { state: 'idle', message: null, startedAt: null }, isLoading: false })),
  useCancelAddAccount: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useAddCopilotAccount: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useCopilotAddStatus: vi.fn(() => ({ data: undefined, isLoading: false })),
  useCancelCopilotAdd: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useAddOpenAIAccount: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useOpenAIAddStatus: vi.fn(() => ({ data: undefined, isLoading: false })),
  useCancelOpenAIAdd: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  QK: {
    accounts: ['accounts'],
    sessions: ['sessions'],
  },
  useAssessDispatch: vi.fn(() => ({ assess: vi.fn() })),
}))

vi.mock('../ReauthBrowserPanel', () => ({
  ReauthBrowserPanel: ({ vncSession }: { vncSession: string }) =>
    <div data-testid="reauth-vnc">{vncSession}</div>,
}))

const NOOP_MUTATION = { mutate: vi.fn(), isPending: false, isSuccess: false }

function makeAccount(overrides: Partial<Account> = {}): Account {
  return {
    name: 'myaccount',
    status: 'ok',
    usage: null,
    liveSessions: { active: 0, idle: 0 },
    rank: null,
    rankReason: null,
    ...overrides,
  }
}

function makeReauthState(overrides: Partial<ReauthState> = {}): ReauthState {
  return { state: 'idle', message: null, startedAt: null, ...overrides }
}

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

function setupDefaultMocks() {
  vi.mocked(hooks.useAccounts).mockReturnValue(queryResult([]))
  vi.mocked(hooks.useSessions).mockReturnValue(queryResult([]))
  vi.mocked(hooks.useRefreshAccount).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useRefreshAccount>)
  vi.mocked(hooks.useUpdateAccount).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useUpdateAccount>)
  vi.mocked(hooks.useDeleteAccount).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useDeleteAccount>)
  vi.mocked(hooks.useReauthAccount).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useReauthAccount>)
  vi.mocked(hooks.useReauthStatus).mockReturnValue(queryResult(makeReauthState()))
  vi.mocked(hooks.useCancelReauth).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useCancelReauth>)
  vi.mocked(hooks.useSubmitReauthCode).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useSubmitReauthCode>)
}

describe('AccountsManager — Copilot provider chip', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    setupDefaultMocks()
  })

  it('does NOT show a "Copilot" chip for an Anthropic account', () => {
    vi.mocked(hooks.useAccounts).mockReturnValue(queryResult([makeAccount({ name: 'work', provider: 'anthropic' })]))

    render(<AccountsManager />, { wrapper })

    expect(screen.queryByText('Copilot')).not.toBeInTheDocument()
  })

  it('shows a "Copilot" chip for a copilot account', () => {
    vi.mocked(hooks.useAccounts).mockReturnValue(queryResult([makeAccount({ name: 'gh-copilot', provider: 'copilot' })]))

    render(<AccountsManager />, { wrapper })

    expect(screen.getByText('Copilot')).toBeInTheDocument()
  })

  it('does NOT show a "Copilot" chip when provider is absent (legacy Anthropic account)', () => {
    vi.mocked(hooks.useAccounts).mockReturnValue(queryResult([makeAccount({ name: 'legacy', provider: undefined })]))

    render(<AccountsManager />, { wrapper })

    expect(screen.queryByText('Copilot')).not.toBeInTheDocument()
  })

  it('shows "Copilot" chips for each copilot account in a mixed list', () => {
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [
        makeAccount({ name: 'anthr', provider: 'anthropic' }),
        makeAccount({ name: 'cop1', provider: 'copilot' }),
        makeAccount({ name: 'cop2', provider: 'copilot' }),
      ],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)

    render(<AccountsManager />, { wrapper })

    // Two copilot chips, one per copilot account
    expect(screen.getAllByText('Copilot')).toHaveLength(2)
  })
})

describe('AccountsManager — Copilot usage badge label', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    setupDefaultMocks()
  })

  it('uses "session" label for Anthropic account usage', () => {
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({
        name: 'work',
        provider: 'anthropic',
        usage: anthropicUsage(50, 30),
      })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)

    render(<AccountsManager />, { wrapper })

    // The UsageBadge renders the label as visible text
    expect(screen.getByText('session')).toBeInTheDocument()
  })

  it('renders one "premium-req" badge for a Copilot account — never a duplicate', () => {
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [makeAccount({
        name: 'gh-copilot',
        provider: 'copilot',
        usage: copilotUsage(50),
      })],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)

    render(<AccountsManager />, { wrapper })

    // The label now comes from the WINDOW the provider reported, not from a
    // provider check in the component.
    expect(screen.getAllByText('premium-req')).toHaveLength(1)
    // Copilot meters one allowance, so neither Anthropic window key appears —
    // the old wire shape duplicated the same number into a second "weekly" badge.
    expect(screen.queryByText('session')).not.toBeInTheDocument()
    expect(screen.queryByText('weekly')).not.toBeInTheDocument()
  })
})

describe('AccountsManager — Copilot reauth button', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    setupDefaultMocks()
  })

  it('shows a reauth button for a copilot account', () => {
    vi.mocked(hooks.useAccounts).mockReturnValue(queryResult([makeAccount({ name: 'gh-copilot', provider: 'copilot' })]))

    render(<AccountsManager />, { wrapper })

    expect(screen.getByRole('button', { name: /re-run device flow for gh-copilot/i })).toBeInTheDocument()
  })

  it('clicking the copilot reauth button opens AddAccountDialog pre-selected on Copilot provider', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useAccounts).mockReturnValue(queryResult([makeAccount({ name: 'gh-copilot', provider: 'copilot' })]))

    render(<AccountsManager />, { wrapper })

    await user.click(screen.getByRole('button', { name: /re-run device flow for gh-copilot/i }))

    // AddAccountDialog should open (renders a dialog)
    expect(screen.getByRole('dialog')).toBeInTheDocument()

    // The "GitHub Copilot" provider toggle button should be pressed (pre-selected)
    const copilotToggle = screen.getByRole('button', { name: /github copilot/i })
    expect(copilotToggle).toHaveAttribute('aria-pressed', 'true')
  })

  it('clicking copilot reauth does NOT call the Anthropic reauthAccount mutation', async () => {
    const reauthMutate = vi.fn()
    vi.mocked(hooks.useReauthAccount).mockReturnValue({
      ...NOOP_MUTATION,
      mutate: reauthMutate,
    } as unknown as ReturnType<typeof hooks.useReauthAccount>)

    vi.mocked(hooks.useAccounts).mockReturnValue(queryResult([makeAccount({ name: 'gh-copilot', provider: 'copilot' })]))

    const user = userEvent.setup()
    render(<AccountsManager />, { wrapper })

    await user.click(screen.getByRole('button', { name: /re-run device flow for gh-copilot/i }))

    expect(reauthMutate).not.toHaveBeenCalled()
  })

  it('the copilot reauth button uses "Re-authenticate" label when account needs-reauth', () => {
    vi.mocked(hooks.useAccounts).mockReturnValue(queryResult([makeAccount({ name: 'gh-copilot', provider: 'copilot', status: 'needs-reauth' })]))

    render(<AccountsManager />, { wrapper })

    // The button text is "Re-authenticate" in the needsReauth state
    const btn = screen.getByRole('button', { name: /re-run device flow for gh-copilot/i })
    expect(btn).toHaveTextContent('Re-authenticate')
  })
})
