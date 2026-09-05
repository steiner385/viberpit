/**
 * AddAccountDialog — the OpenAI (ChatGPT) device flow.
 *
 * Same state machine as Copilot, deliberately: the assertions below are the
 * Copilot ones re-pointed, because a divergence between the two panels is the
 * bug this shared `DeviceCodePanel` exists to prevent.
 *
 * The last test is a security invariant, not a UI one: a device code is a live
 * credential-adjacent secret for its short life, and nothing may persist it.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { AddAccountDialog } from '../AddAccountDialog'
import * as hooks from '@/api/hooks'
import type { DeviceCodeState } from '@/api/types'

vi.mock('@/api/hooks', () => ({
  useAddAccount: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useAddAccountStatus: vi.fn(() => ({ data: { state: 'idle', message: null, startedAt: null }, isLoading: false })),
  useCancelAddAccount: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useAddCopilotAccount: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useCopilotAddStatus: vi.fn(() => ({ data: undefined, isLoading: false })),
  useCancelCopilotAdd: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useAddOpenAIAccount: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useOpenAIAddStatus: vi.fn(() => ({ data: undefined, isLoading: false })),
  useCancelOpenAIAdd: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  QK: { accounts: ['accounts'] },
  useAssessDispatch: vi.fn(() => ({ assess: vi.fn() })),
}))

vi.mock('../ReauthBrowserPanel', () => ({
  ReauthBrowserPanel: () => <div data-testid="reauth-panel" />,
}))

const USER_CODE = 'WXYZ-9876'
const VERIFY_URI = 'https://chatgpt.com/device'

function deviceState(overrides: Partial<DeviceCodeState> = {}): DeviceCodeState {
  return { state: 'pending', userCode: null, verificationUri: null, ...overrides }
}

function mockOpenAIStatus(state: DeviceCodeState) {
  vi.mocked(hooks.useOpenAIAddStatus).mockImplementation((_name: string, enabled: boolean) => ({
    data: enabled ? state : undefined,
    isLoading: false,
  } as ReturnType<typeof hooks.useOpenAIAddStatus>))
}

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

/** Opens the dialog, picks OpenAI, names the account and starts the flow. */
async function startFlow(user: ReturnType<typeof userEvent.setup>, onClose = vi.fn()) {
  render(<AddAccountDialog onClose={onClose} />, { wrapper })
  await user.click(screen.getByRole('button', { name: /openai \(chatgpt\)/i }))
  await user.type(screen.getByLabelText(/account name/i), 'chatgpt')
  await user.click(screen.getByRole('button', { name: /^add$/i }))
  return onClose
}

beforeEach(() => {
  vi.clearAllMocks()
  window.localStorage.clear()
})

describe('AddAccountDialog — OpenAI (ChatGPT) device flow', () => {
  it('offers OpenAI as a third provider', () => {
    render(<AddAccountDialog onClose={vi.fn()} />, { wrapper })
    const tab = screen.getByRole('button', { name: /openai \(chatgpt\)/i })
    expect(tab).toBeInTheDocument()
    expect(tab).toHaveAttribute('aria-pressed', 'false')
  })

  it('can be pre-selected (the reauth entry point)', () => {
    render(<AddAccountDialog onClose={vi.fn()} initialProvider="openai" initialName="chatgpt" />,
      { wrapper })
    expect(screen.getByRole('button', { name: /openai \(chatgpt\)/i }))
      .toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByLabelText(/account name/i)).toHaveValue('chatgpt')
  })

  it('POSTs the OpenAI add with the trimmed name', async () => {
    const user = userEvent.setup()
    const addMutate = vi.fn()
    vi.mocked(hooks.useAddOpenAIAccount).mockReturnValue(
      { mutate: addMutate, isPending: false } as unknown as ReturnType<typeof hooks.useAddOpenAIAccount>)
    await startFlow(user)
    expect(addMutate).toHaveBeenCalledWith('chatgpt', expect.anything())
  })

  it('shows the device code and the verification link', async () => {
    const user = userEvent.setup()
    mockOpenAIStatus(deviceState({
      state: 'awaiting_authorization', userCode: USER_CODE, verificationUri: VERIFY_URI,
    }))
    await startFlow(user)

    expect(screen.getByText(USER_CODE)).toBeInTheDocument()
    const link = screen.getByRole('link', { name: /chatgpt\.com\/device/i })
    expect(link).toHaveAttribute('href', VERIFY_URI)
    expect(link).toHaveAttribute('rel', expect.stringContaining('noopener'))
  })

  it('narrates the pending state and reports a failure with the server reason', async () => {
    const user = userEvent.setup()
    mockOpenAIStatus(deviceState({ state: 'failed', error: 'authority is unreachable' }))
    await startFlow(user)
    expect(await screen.findByRole('alert')).toHaveTextContent('authority is unreachable')
  })

  it('closes once the login is authorized', async () => {
    const user = userEvent.setup()
    mockOpenAIStatus(deviceState({ state: 'authorized' }))
    const onClose = await startFlow(user)
    await waitFor(() => expect(onClose).toHaveBeenCalled())
  })

  it('cancels through the OpenAI cancel route, not the Copilot one', async () => {
    const user = userEvent.setup()
    const openaiCancel = vi.fn()
    const copilotCancel = vi.fn()
    vi.mocked(hooks.useCancelOpenAIAdd).mockReturnValue(
      { mutate: openaiCancel, isPending: false } as unknown as ReturnType<typeof hooks.useCancelOpenAIAdd>)
    vi.mocked(hooks.useCancelCopilotAdd).mockReturnValue(
      { mutate: copilotCancel, isPending: false } as unknown as ReturnType<typeof hooks.useCancelCopilotAdd>)
    mockOpenAIStatus(deviceState({
      state: 'awaiting_authorization', userCode: USER_CODE, verificationUri: VERIFY_URI,
    }))
    await startFlow(user)

    await user.click(screen.getByRole('button', { name: /cancel add/i }))
    expect(openaiCancel).toHaveBeenCalledWith('chatgpt')
    expect(copilotCancel).not.toHaveBeenCalled()
  })

  it('switching provider abandons the in-flight OpenAI flow', async () => {
    const user = userEvent.setup()
    mockOpenAIStatus(deviceState({
      state: 'awaiting_authorization', userCode: USER_CODE, verificationUri: VERIFY_URI,
    }))
    await startFlow(user)
    expect(screen.getByText(USER_CODE)).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /github copilot/i }))
    // The code belongs to a flow the user can no longer see — leaving it on
    // screen (or polling behind it) is both confusing and needless exposure.
    expect(screen.queryByText(USER_CODE)).not.toBeInTheDocument()
  })

  it('never writes the device code or verification URI to localStorage', async () => {
    // Security: `userCode` is short-lived, single-use and must not reach query
    // cache persistence, analytics, or an error/telemetry payload. localStorage
    // is the observable proxy for all of those in jsdom.
    const user = userEvent.setup()
    const setItem = vi.spyOn(Storage.prototype, 'setItem')
    mockOpenAIStatus(deviceState({
      state: 'awaiting_authorization', userCode: USER_CODE, verificationUri: VERIFY_URI,
    }))
    await startFlow(user)
    expect(screen.getByText(USER_CODE)).toBeInTheDocument()

    for (const [, value] of setItem.mock.calls) {
      expect(String(value)).not.toContain(USER_CODE)
      expect(String(value)).not.toContain(VERIFY_URI)
    }
    const dump = JSON.stringify(window.localStorage)
    expect(dump).not.toContain(USER_CODE)
    expect(dump).not.toContain(VERIFY_URI)
    setItem.mockRestore()
  })
})
