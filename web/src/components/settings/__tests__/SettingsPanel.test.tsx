import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { SettingsPanel } from '../SettingsPanel'
import * as hooks from '@/api/hooks'

vi.mock('@/api/hooks', () => ({
  useAdoptable: vi.fn(() => ({ data: { candidates: [] } })),
  useAdoptSession: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useSettings: vi.fn(),
  useUpdateSettings: vi.fn(),
}))

const SETTINGS = {
  defaultPermissionMode: 'interactive',
  thinkingCollapsed: false,
  repoRoot: '/home/user/repos',
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

describe('SettingsPanel', () => {
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

  it('renders as a dialog with the correct aria attributes', () => {
    render(<SettingsPanel onClose={vi.fn()} />, { wrapper })
    const dialog = screen.getByRole('dialog', { name: /settings/i })
    expect(dialog).toBeInTheDocument()
    expect(dialog).toHaveAttribute('aria-modal', 'true')
  })

  it('renders the default tool approvals select pre-filled with current value', () => {
    render(<SettingsPanel onClose={vi.fn()} />, { wrapper })
    const select = screen.getByLabelText(/default tool approvals/i)
    expect(select).toBeInTheDocument()
    expect((select as HTMLSelectElement).value).toBe('interactive')
  })

  it('renders all four permission mode options', () => {
    render(<SettingsPanel onClose={vi.fn()} />, { wrapper })
    expect(screen.getByRole('option', { name: /ask each time/i })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: /auto-approve safe tools/i })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: /allowlist read-only/i })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: /full auto/i })).toBeInTheDocument()
  })

  it('renders the thinkingCollapsed checkbox pre-filled', () => {
    render(<SettingsPanel onClose={vi.fn()} />, { wrapper })
    const checkbox = screen.getByLabelText(/collapse thinking by default/i)
    expect(checkbox).toBeInTheDocument()
    expect((checkbox as HTMLInputElement).checked).toBe(false)
  })

  it('renders the repo clone root input pre-filled', () => {
    render(<SettingsPanel onClose={vi.fn()} />, { wrapper })
    const input = screen.getByLabelText(/repo clone root/i)
    expect(input).toBeInTheDocument()
    expect((input as HTMLInputElement).value).toBe('/home/user/repos')
  })

  it('does NOT show the full-auto warning when interactive is selected', () => {
    render(<SettingsPanel onClose={vi.fn()} />, { wrapper })
    expect(screen.queryByText(/full auto lets sessions run/i)).not.toBeInTheDocument()
  })

  it('shows the full-auto warning when full-auto is selected', async () => {
    const user = userEvent.setup()
    render(<SettingsPanel onClose={vi.fn()} />, { wrapper })

    await user.selectOptions(screen.getByLabelText(/default tool approvals/i), 'full-auto')

    expect(screen.getByText(/full auto lets sessions run/i)).toBeInTheDocument()
  })

  it('calls mutate with changed fields when Save is clicked', async () => {
    const mutate = vi.fn()
    vi.mocked(hooks.useUpdateSettings).mockReturnValue({
      ...NOOP_MUTATION,
      mutate,
    } as unknown as ReturnType<typeof hooks.useUpdateSettings>)

    const user = userEvent.setup()
    render(<SettingsPanel onClose={vi.fn()} />, { wrapper })

    await user.selectOptions(screen.getByLabelText(/default tool approvals/i), 'auto')
    await user.click(screen.getByRole('button', { name: /save/i }))

    expect(mutate).toHaveBeenCalledWith(
      expect.objectContaining({ defaultPermissionMode: 'auto' }),
      expect.anything(),
    )
  })

  it('reflects server values when settings load AFTER the panel mounts', () => {
    // Open before the settings query resolves: form shows defaults.
    vi.mocked(hooks.useSettings).mockReturnValue({
      data: undefined,
      isLoading: true,
    } as ReturnType<typeof hooks.useSettings>)

    const { rerender } = render(<SettingsPanel onClose={vi.fn()} />, { wrapper })
    expect((screen.getByLabelText(/repo clone root/i) as HTMLInputElement).value).toBe('')

    // Settings resolve: the form must pick up the server values.
    vi.mocked(hooks.useSettings).mockReturnValue({
      data: SETTINGS,
      isLoading: false,
    } as ReturnType<typeof hooks.useSettings>)
    rerender(<SettingsPanel onClose={vi.fn()} />)

    expect((screen.getByLabelText(/repo clone root/i) as HTMLInputElement).value).toBe('/home/user/repos')
    expect((screen.getByLabelText(/default tool approvals/i) as HTMLSelectElement).value).toBe('interactive')
  })

  it('calls onClose when the Close button is clicked', async () => {
    const onClose = vi.fn()
    const user = userEvent.setup()
    render(<SettingsPanel onClose={onClose} />, { wrapper })

    await user.click(screen.getByRole('button', { name: /close/i }))

    expect(onClose).toHaveBeenCalledOnce()
  })

  it('calls onClose when Escape is pressed', async () => {
    const onClose = vi.fn()
    const user = userEvent.setup()
    render(<SettingsPanel onClose={onClose} />, { wrapper })

    await user.keyboard('{Escape}')

    expect(onClose).toHaveBeenCalledOnce()
  })
})
