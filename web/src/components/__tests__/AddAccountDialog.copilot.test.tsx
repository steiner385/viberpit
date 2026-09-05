import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, render, screen, waitFor } from '@testing-library/react'
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

// Also mock ReauthBrowserPanel since it needs a VNC connection
vi.mock('../ReauthBrowserPanel', () => ({
  ReauthBrowserPanel: () => <div data-testid="reauth-panel" />,
}))

function makeCopilotState(overrides: Partial<DeviceCodeState> = {}): DeviceCodeState {
  return { state: 'pending', userCode: null, verificationUri: null, ...overrides }
}

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

describe('AddAccountDialog — GitHub Copilot flow', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    const mutate = vi.fn((
      _name: string,
      options?: { onSuccess?: () => void },
    ) => { options?.onSuccess?.() })
    vi.mocked(hooks.useAddCopilotAccount).mockReturnValue(
      { mutate, isPending: false } as unknown as ReturnType<typeof hooks.useAddCopilotAccount>)
  })

  it('shows userCode and verification link when awaiting_authorization', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useCopilotAddStatus).mockImplementation((_name: string, enabled: boolean) => ({
      data: enabled ? makeCopilotState({
        state: 'awaiting_authorization',
        userCode: 'ABCD-1234',
        verificationUri: 'https://github.com/login/device',
      }) : undefined,
      isLoading: false,
    } as ReturnType<typeof hooks.useCopilotAddStatus>))

    const addMutate = vi.fn((
      _name: string,
      options?: { onSuccess?: () => void },
    ) => { options?.onSuccess?.() })
    vi.mocked(hooks.useAddCopilotAccount).mockReturnValue({
      mutate: addMutate,
      isPending: false,
    } as unknown as ReturnType<typeof hooks.useAddCopilotAccount>)

    const onClose = vi.fn()
    render(<AddAccountDialog onClose={onClose} />, { wrapper })

    // Select GitHub Copilot provider
    await user.click(screen.getByRole('button', { name: /github copilot/i }))

    // Enter account name
    await user.type(screen.getByLabelText(/account name/i), 'mycopilot')

    // Submit
    await user.click(screen.getByRole('button', { name: /^add$/i }))

    // Should show device code and link
    expect(screen.getByText('ABCD-1234')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /github\.com\/login\/device/i })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /github\.com\/login\/device/i })).toHaveAttribute('href', 'https://github.com/login/device')
  })

  it('waits for the start POST before polling a previous terminal state', async () => {
    const pollEnabled: boolean[] = []
    let finishStart: (() => void) | undefined
    vi.mocked(hooks.useCopilotAddStatus).mockImplementation((_name: string, enabled: boolean) => {
      pollEnabled.push(enabled)
      return {
        data: enabled ? makeCopilotState({ state: 'failed', error: 'cancelled' }) : undefined,
        isLoading: false,
      } as ReturnType<typeof hooks.useCopilotAddStatus>
    })
    const mutate = vi.fn((
      _name: string,
      options?: { onSuccess?: () => void },
    ) => { finishStart = options?.onSuccess })
    vi.mocked(hooks.useAddCopilotAccount).mockReturnValue(
      { mutate, isPending: false } as unknown as ReturnType<typeof hooks.useAddCopilotAccount>)

    const user = userEvent.setup()
    render(<AddAccountDialog onClose={vi.fn()} initialProvider="copilot" />, { wrapper })
    await user.type(screen.getByLabelText(/account name/i), 'mycopilot')
    await user.click(screen.getByRole('button', { name: /^add$/i }))

    expect(pollEnabled.at(-1)).toBe(false)
    act(() => finishStart?.())
    await waitFor(() => expect(pollEnabled.at(-1)).toBe(true))
  })

  it('calls onClose when copilot status reaches authorized', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useCopilotAddStatus).mockImplementation((_name: string, enabled: boolean) => ({
      data: enabled ? makeCopilotState({ state: 'authorized' }) : undefined,
      isLoading: false,
    } as ReturnType<typeof hooks.useCopilotAddStatus>))

    const onClose = vi.fn()
    render(<AddAccountDialog onClose={onClose} />, { wrapper })

    // Select GitHub Copilot provider
    await user.click(screen.getByRole('button', { name: /github copilot/i }))

    // Enter account name and submit
    await user.type(screen.getByLabelText(/account name/i), 'mycopilot')
    await user.click(screen.getByRole('button', { name: /^add$/i }))

    await waitFor(() => {
      expect(onClose).toHaveBeenCalled()
    })
  })

  it('cancels the in-flight Copilot flow when switching provider', async () => {
    const user = userEvent.setup()
    const cancelMutate = vi.fn()
    vi.mocked(hooks.useCancelCopilotAdd).mockReturnValue({
      mutate: cancelMutate,
      isPending: false,
    } as unknown as ReturnType<typeof hooks.useCancelCopilotAdd>)
    const onClose = vi.fn()
    render(<AddAccountDialog onClose={onClose} initialProvider="copilot" />, { wrapper })

    await user.type(screen.getByLabelText(/account name/i), 'mycopilot')
    await user.click(screen.getByRole('button', { name: /^add$/i }))
    await user.click(screen.getByRole('button', { name: /anthropic \(oauth\)/i }))

    expect(cancelMutate).toHaveBeenCalledWith('mycopilot')
  })
})
