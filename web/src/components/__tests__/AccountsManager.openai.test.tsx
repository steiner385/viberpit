/**
 * AccountsManager — the OpenAI/Codex account row.
 *
 * Three things an operator cannot fix without: which accounts are OpenAI, is
 * the credential authority actually reachable, and how many runners are holding
 * this account's credential right now. Plus the reauth control, which for an
 * OpenAI account is a device flow at the authority — NOT the Anthropic browser
 * OAuth, and not the Copilot add route (which 409s on an existing name).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { AccountsManager } from '../AccountsManager'
import * as hooks from '@/api/hooks'
import type { Account, DeviceCodeState, ReauthState } from '@/api/types'
import { queryResult } from '@/test/reactQuery'
import { account, codexUsage } from '@/test/factories'

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
  useOpenAIReauth: vi.fn(),
  useOpenAIReauthStatus: vi.fn(),
  useCancelOpenAIReauth: vi.fn(),
  useAddAccount: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useAddAccountStatus: vi.fn(() => ({ data: { state: 'idle', message: null, startedAt: null }, isLoading: false })),
  useCancelAddAccount: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useAddCopilotAccount: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useCopilotAddStatus: vi.fn(() => ({ data: undefined, isLoading: false })),
  useCancelCopilotAdd: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useAddOpenAIAccount: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useOpenAIAddStatus: vi.fn(() => ({ data: undefined, isLoading: false })),
  useCancelOpenAIAdd: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  QK: { accounts: ['accounts'], sessions: ['sessions'] },
  useAssessDispatch: vi.fn(() => ({ assess: vi.fn() })),
}))

vi.mock('../ReauthBrowserPanel', () => ({
  ReauthBrowserPanel: ({ vncSession }: { vncSession: string }) =>
    <div data-testid="reauth-vnc">{vncSession}</div>,
}))

const NOOP_MUTATION = { mutate: vi.fn(), isPending: false, isSuccess: false }

function openaiAccount(over: Partial<Account> = {}): Account {
  return account({
    name: 'chatgpt',
    provider: 'openai',
    engine: 'codex',
    userAuthMode: 'chatgpt_device',
    runnerAuthMode: 'chatgpt_external_tokens',
    credentialAuthority: 'codex_authority',
    usage: codexUsage(40),
    codex: {
      authority: {
        reachable: true, protocolVersion: 1, syncRevision: 7,
        syncExpiresInS: 600, accounts: 1, detail: null,
      },
      quota: codexUsage(40),
      activeRunners: 2,
      degradedReason: null,
    },
    ...over,
  })
}

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

function mockDeviceStatus(state?: DeviceCodeState) {
  vi.mocked(hooks.useOpenAIReauthStatus).mockImplementation((_name: string, enabled: boolean) => ({
    data: enabled ? state : undefined,
    isLoading: false,
  } as ReturnType<typeof hooks.useOpenAIReauthStatus>))
}

function setupDefaultMocks() {
  vi.mocked(hooks.useAccounts).mockReturnValue(queryResult([]))
  vi.mocked(hooks.useSessions).mockReturnValue(queryResult([]))
  // Spelled out rather than looped: each hook's mutation variables differ, so a
  // loop collapses them into a union TS cannot satisfy with one stub.
  vi.mocked(hooks.useRefreshAccount).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useRefreshAccount>)
  vi.mocked(hooks.useUpdateAccount).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useUpdateAccount>)
  vi.mocked(hooks.useDeleteAccount).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useDeleteAccount>)
  vi.mocked(hooks.useReauthAccount).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useReauthAccount>)
  vi.mocked(hooks.useCancelReauth).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useCancelReauth>)
  vi.mocked(hooks.useSubmitReauthCode).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useSubmitReauthCode>)
  vi.mocked(hooks.useOpenAIReauth).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useOpenAIReauth>)
  vi.mocked(hooks.useCancelOpenAIReauth).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useCancelOpenAIReauth>)
  vi.mocked(hooks.useReauthStatus).mockReturnValue(
    queryResult<ReauthState>({ state: 'idle', message: null, startedAt: null }))
  mockDeviceStatus(undefined)
}

beforeEach(() => {
  vi.clearAllMocks()
  setupDefaultMocks()
})

describe('AccountsManager — the OpenAI chip', () => {
  it('marks an OpenAI account, and only that one', () => {
    vi.mocked(hooks.useAccounts).mockReturnValue(queryResult([
      openaiAccount(),
      account({ name: 'work', provider: 'anthropic' }),
      account({ name: 'gh', provider: 'copilot' }),
    ]))
    render(<AccountsManager />, { wrapper })
    expect(screen.getAllByText('OpenAI')).toHaveLength(1)
    expect(screen.getByText('Copilot')).toBeInTheDocument()
  })
})

describe('AccountsManager — Codex authority health and runners', () => {
  it('reports a reachable authority and the live runner count', () => {
    vi.mocked(hooks.useAccounts).mockReturnValue(queryResult([openaiAccount()]))
    render(<AccountsManager />, { wrapper })
    expect(screen.getByText(/authority reachable/i)).toBeInTheDocument()
    expect(screen.getByText(/2 active runners/i)).toBeInTheDocument()
  })

  it('says the authority is unreachable, and why the account is degraded', () => {
    vi.mocked(hooks.useAccounts).mockReturnValue(queryResult([openaiAccount({
      codex: {
        authority: { reachable: false, detail: 'connection refused' },
        quota: null,
        activeRunners: 0,
        degradedReason: 'authority_unreachable',
      },
    })]))
    render(<AccountsManager />, { wrapper })
    expect(screen.getByText(/authority unreachable/i)).toBeInTheDocument()
    expect(screen.getByText(/authority_unreachable/i)).toBeInTheDocument()
  })

  it('says so when the edge has no authority configured at all', () => {
    // `authority: null` is "not configured / probe failed", NOT "down" — the
    // same tri-state discipline host health uses.
    vi.mocked(hooks.useAccounts).mockReturnValue(queryResult([openaiAccount({
      codex: { authority: null, quota: null, activeRunners: 0, degradedReason: null },
    })]))
    render(<AccountsManager />, { wrapper })
    expect(screen.getByText(/authority status unknown/i)).toBeInTheDocument()
  })

  it('renders quota through the generic window badges, never a re-derived pair', () => {
    vi.mocked(hooks.useAccounts).mockReturnValue(queryResult([openaiAccount()]))
    render(<AccountsManager />, { wrapper })
    // One badge per window the provider actually reported.
    expect(screen.getAllByText('premium')).toHaveLength(1)
    expect(screen.queryByText('weekly')).not.toBeInTheDocument()
  })

  it('shows no Codex line for a non-OpenAI account', () => {
    vi.mocked(hooks.useAccounts).mockReturnValue(queryResult([account({ name: 'work' })]))
    render(<AccountsManager />, { wrapper })
    expect(screen.queryByText(/authority/i)).not.toBeInTheDocument()
  })
})

describe('AccountsManager — OpenAI reauth is the device flow', () => {
  it('starts the OpenAI device flow, not the Anthropic browser OAuth', async () => {
    const openaiStart = vi.fn()
    const anthropicStart = vi.fn()
    vi.mocked(hooks.useOpenAIReauth).mockReturnValue(
      { ...NOOP_MUTATION, mutate: openaiStart } as unknown as ReturnType<typeof hooks.useOpenAIReauth>)
    vi.mocked(hooks.useReauthAccount).mockReturnValue(
      { ...NOOP_MUTATION, mutate: anthropicStart } as unknown as ReturnType<typeof hooks.useReauthAccount>)
    vi.mocked(hooks.useAccounts).mockReturnValue(queryResult([openaiAccount()]))

    const user = userEvent.setup()
    render(<AccountsManager />, { wrapper })
    await user.click(screen.getByRole('button', { name: /re-run device flow for chatgpt/i }))

    expect(openaiStart).toHaveBeenCalledWith('chatgpt', expect.anything())
    expect(anthropicStart).not.toHaveBeenCalled()
  })

  it('shows the device code inline once the authority issues one', async () => {
    mockDeviceStatus({
      state: 'awaiting_authorization', userCode: 'WXYZ-9876',
      verificationUri: 'https://chatgpt.com/device',
    })
    vi.mocked(hooks.useAccounts).mockReturnValue(queryResult([openaiAccount()]))

    const user = userEvent.setup()
    render(<AccountsManager />, { wrapper })
    await user.click(screen.getByRole('button', { name: /re-run device flow for chatgpt/i }))

    expect(screen.getByText('WXYZ-9876')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /chatgpt\.com\/device/i })).toBeInTheDocument()
  })

  it('cancels through the OpenAI cancel route', async () => {
    const openaiCancel = vi.fn()
    vi.mocked(hooks.useCancelOpenAIReauth).mockReturnValue(
      { ...NOOP_MUTATION, mutate: openaiCancel } as unknown as ReturnType<typeof hooks.useCancelOpenAIReauth>)
    mockDeviceStatus({
      state: 'awaiting_authorization', userCode: 'WXYZ-9876',
      verificationUri: 'https://chatgpt.com/device',
    })
    vi.mocked(hooks.useAccounts).mockReturnValue(queryResult([openaiAccount()]))

    const user = userEvent.setup()
    render(<AccountsManager />, { wrapper })
    await user.click(screen.getByRole('button', { name: /re-run device flow for chatgpt/i }))
    await user.click(screen.getByRole('button', { name: /cancel reauth/i }))

    expect(openaiCancel).toHaveBeenCalledWith('chatgpt')
  })

  it('never writes the device code to localStorage', async () => {
    const setItem = vi.spyOn(Storage.prototype, 'setItem')
    mockDeviceStatus({
      state: 'awaiting_authorization', userCode: 'WXYZ-9876',
      verificationUri: 'https://chatgpt.com/device',
    })
    vi.mocked(hooks.useAccounts).mockReturnValue(queryResult([openaiAccount()]))

    const user = userEvent.setup()
    render(<AccountsManager />, { wrapper })
    await user.click(screen.getByRole('button', { name: /re-run device flow for chatgpt/i }))
    expect(screen.getByText('WXYZ-9876')).toBeInTheDocument()

    for (const [, value] of setItem.mock.calls) {
      expect(String(value)).not.toContain('WXYZ-9876')
    }
    expect(JSON.stringify(window.localStorage)).not.toContain('WXYZ-9876')
    setItem.mockRestore()
  })
})
