import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { GeneralSection } from '../GeneralSection'
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
}))

const SETTINGS = {
  defaultPermissionMode: 'interactive',
  thinkingCollapsed: false,
  repoRoot: '/home/user/repos',
  defaultToolDetail: 'auto',
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

describe('GeneralSection', () => {
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

  it('renders inline (no dialog role)', () => {
    render(<GeneralSection />, { wrapper })
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.getByLabelText(/default tool approvals/i)).toBeInTheDocument()
  })

  it('pre-fills fields from settings', () => {
    render(<GeneralSection />, { wrapper })
    expect((screen.getByLabelText(/default tool approvals/i) as HTMLSelectElement).value).toBe('interactive')
    expect((screen.getByLabelText(/repo clone root/i) as HTMLInputElement).value).toBe('/home/user/repos')
    expect((screen.getByLabelText(/collapse thinking by default/i) as HTMLInputElement).checked).toBe(false)
  })

  it('shows the full-auto warning only when full-auto is selected', async () => {
    const user = userEvent.setup()
    render(<GeneralSection />, { wrapper })
    expect(screen.queryByText(/full auto lets sessions run/i)).not.toBeInTheDocument()
    await user.selectOptions(screen.getByLabelText(/default tool approvals/i), 'full-auto')
    expect(screen.getByText(/full auto lets sessions run/i)).toBeInTheDocument()
  })

  it('saves changed fields', async () => {
    const mutate = vi.fn()
    vi.mocked(hooks.useUpdateSettings).mockReturnValue({ ...NOOP_MUTATION, mutate } as unknown as ReturnType<typeof hooks.useUpdateSettings>)
    const user = userEvent.setup()
    render(<GeneralSection />, { wrapper })
    await user.selectOptions(screen.getByLabelText(/default tool approvals/i), 'full-auto')
    await user.click(screen.getByRole('button', { name: /save/i }))
    expect(mutate).toHaveBeenCalledWith(
      expect.objectContaining({ defaultPermissionMode: 'full-auto', repoRoot: '/home/user/repos' }),
      expect.anything(),
    )
  })
})
