/**
 * Tests for model-approval policy controls:
 *   - GeneralSection: global modelApprovalAction select + modelApprovalDenylist input
 *   - AccountsManager: per-account modelApprovalAction override select
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { GeneralSection } from '../GeneralSection'
import { AccountsManager } from '../../AccountsManager'
import * as hooks from '@/api/hooks'

vi.mock('@/api/hooks', () => ({
  useAdoptable: vi.fn(() => ({ data: { candidates: [] } })),
  useAdoptSession: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useSettings: vi.fn(),
  useUpdateSettings: vi.fn(),
  useSessionsViewing: vi.fn(),
  useAddCopilotAccount: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useCopilotAddStatus: vi.fn(() => ({ data: undefined, isLoading: false })),
  useCancelCopilotAdd: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useAssessDispatch: vi.fn(() => ({ assess: vi.fn() })),
  // AccountsManager deps
  useAccounts: vi.fn(),
  useSessions: vi.fn(() => ({ data: [], isLoading: false })),
  useRefreshAccount: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useUpdateAccount: vi.fn(),
  useDeleteAccount: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useSetAccountEnabled: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useReauthAccount: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useReauthStatus: vi.fn(() => ({ data: { state: 'idle', message: null, startedAt: null }, isLoading: false })),
  useCancelReauth: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useSubmitReauthCode: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  QK: {
    accounts: ['accounts'],
    sessions: ['sessions'],
    settings: ['settings'],
  },
}))

const SETTINGS = {
  defaultPermissionMode: 'interactive',
  thinkingCollapsed: false,
  repoRoot: null,
  defaultToolDetail: 'auto',
  maxArchivedSessions: null,
  autoArchiveEnabled: true,
  modelApprovalAction: null,
  modelApprovalDenylist: null,
}

const NOOP_MUTATION = {
  mutate: vi.fn(),
  isPending: false,
  isSuccess: false,
}

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

// ── GeneralSection: model-approval controls ──────────────────────────────────

describe('GeneralSection — model approval controls', () => {
  beforeEach(() => {
    vi.mocked(hooks.useSettings).mockReturnValue({
      data: SETTINGS,
      isLoading: false,
    } as ReturnType<typeof hooks.useSettings>)

    vi.mocked(hooks.useUpdateSettings).mockReturnValue({
      ...NOOP_MUTATION,
      mutate: vi.fn(),
    } as unknown as ReturnType<typeof hooks.useUpdateSettings>)
  })

  it('renders the model approval action select', () => {
    render(<GeneralSection />, { wrapper })
    expect(screen.getByLabelText(/action for gated models/i)).toBeInTheDocument()
  })

  it('renders the model approval denylist input', () => {
    render(<GeneralSection />, { wrapper })
    expect(screen.getByLabelText(/approval denylist/i)).toBeInTheDocument()
  })

  it('defaults the action select to empty (server default) when settings has null', () => {
    render(<GeneralSection />, { wrapper })
    const select = screen.getByLabelText(/action for gated models/i) as HTMLSelectElement
    expect(select.value).toBe('')
  })

  it('pre-fills action select from settings when non-null', () => {
    vi.mocked(hooks.useSettings).mockReturnValue({
      data: { ...SETTINGS, modelApprovalAction: 'block' },
      isLoading: false,
    } as ReturnType<typeof hooks.useSettings>)
    render(<GeneralSection />, { wrapper })
    const select = screen.getByLabelText(/action for gated models/i) as HTMLSelectElement
    expect(select.value).toBe('block')
  })

  it('sends modelApprovalAction in the save payload', async () => {
    const mutate = vi.fn()
    vi.mocked(hooks.useUpdateSettings).mockReturnValue({ ...NOOP_MUTATION, mutate } as unknown as ReturnType<typeof hooks.useUpdateSettings>)
    const user = userEvent.setup()
    render(<GeneralSection />, { wrapper })
    await user.selectOptions(screen.getByLabelText(/action for gated models/i), 'block')
    await user.click(screen.getByRole('button', { name: /save/i }))
    expect(mutate).toHaveBeenCalledWith(
      expect.objectContaining({ modelApprovalAction: 'block' }),
      expect.anything(),
    )
  })

  it('sends null for modelApprovalAction when left on server default', async () => {
    const mutate = vi.fn()
    vi.mocked(hooks.useUpdateSettings).mockReturnValue({ ...NOOP_MUTATION, mutate } as unknown as ReturnType<typeof hooks.useUpdateSettings>)
    const user = userEvent.setup()
    render(<GeneralSection />, { wrapper })
    await user.click(screen.getByRole('button', { name: /save/i }))
    expect(mutate).toHaveBeenCalledWith(
      expect.objectContaining({ modelApprovalAction: null }),
      expect.anything(),
    )
  })

  it('sends modelApprovalDenylist in the save payload', async () => {
    const mutate = vi.fn()
    vi.mocked(hooks.useUpdateSettings).mockReturnValue({ ...NOOP_MUTATION, mutate } as unknown as ReturnType<typeof hooks.useUpdateSettings>)
    const user = userEvent.setup()
    render(<GeneralSection />, { wrapper })
    await user.type(screen.getByLabelText(/approval denylist/i), 'fable,claude-opus-5')
    await user.click(screen.getByRole('button', { name: /save/i }))
    expect(mutate).toHaveBeenCalledWith(
      expect.objectContaining({ modelApprovalDenylist: 'fable,claude-opus-5' }),
      expect.anything(),
    )
  })
})

// ── AccountsManager: per-account model approval override ─────────────────────

describe('AccountsManager — per-account model approval override', () => {
  const ACCOUNT = {
    name: 'alice@example.com',
    status: 'ok' as const,
    usage: null,
    liveSessions: { active: 0, idle: 0 },
    rank: null,
    rankReason: null,
    accountDefaults: {
      defaultModel: null,
      defaultPermissionMode: null,
      envOverrides: null,
      modelApprovalAction: null,
    },
  }

  beforeEach(() => {
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [ACCOUNT],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)

    vi.mocked(hooks.useUpdateAccount).mockReturnValue({
      ...NOOP_MUTATION,
      mutate: vi.fn(),
    } as unknown as ReturnType<typeof hooks.useUpdateAccount>)
  })

  it('renders the model approval select for each account', () => {
    render(<AccountsManager />, { wrapper })
    expect(screen.getByLabelText(/model approval action for alice@example\.com/i)).toBeInTheDocument()
  })

  it('defaults to Inherit when accountDefaults.modelApprovalAction is null', () => {
    render(<AccountsManager />, { wrapper })
    const select = screen.getByLabelText(/model approval action for alice@example\.com/i) as HTMLSelectElement
    expect(select.value).toBe('')
  })

  it('pre-fills from accountDefaults when non-null', () => {
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [{ ...ACCOUNT, accountDefaults: { ...ACCOUNT.accountDefaults!, modelApprovalAction: 'allow' } }],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)
    render(<AccountsManager />, { wrapper })
    const select = screen.getByLabelText(/model approval action for alice@example\.com/i) as HTMLSelectElement
    expect(select.value).toBe('allow')
  })

  it('calls updateAccount with modelApprovalAction on change', async () => {
    const mutate = vi.fn()
    vi.mocked(hooks.useUpdateAccount).mockReturnValue({ ...NOOP_MUTATION, mutate } as unknown as ReturnType<typeof hooks.useUpdateAccount>)
    const user = userEvent.setup()
    render(<AccountsManager />, { wrapper })
    await user.selectOptions(
      screen.getByLabelText(/model approval action for alice@example\.com/i),
      'require_approval',
    )
    expect(mutate).toHaveBeenCalledWith(
      expect.objectContaining({ name: 'alice@example.com', modelApprovalAction: 'require_approval' }),
    )
  })

  it('sends null for modelApprovalAction when Inherit is selected', async () => {
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [{ ...ACCOUNT, accountDefaults: { ...ACCOUNT.accountDefaults!, modelApprovalAction: 'block' } }],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)
    const mutate = vi.fn()
    vi.mocked(hooks.useUpdateAccount).mockReturnValue({ ...NOOP_MUTATION, mutate } as unknown as ReturnType<typeof hooks.useUpdateAccount>)
    const user = userEvent.setup()
    render(<AccountsManager />, { wrapper })
    await user.selectOptions(
      screen.getByLabelText(/model approval action for alice@example\.com/i),
      '',
    )
    expect(mutate).toHaveBeenCalledWith(
      expect.objectContaining({ name: 'alice@example.com', modelApprovalAction: null }),
    )
  })
})
