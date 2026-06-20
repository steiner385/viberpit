import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Dashboard } from '../Dashboard'
import * as hooks from '@/api/hooks'

vi.mock('@/api/hooks', () => ({
  useAccounts: vi.fn(),
  useSessions: vi.fn(),
  useCreateSession: vi.fn(),
  useRenameSession: vi.fn(),
  useCloseSession: vi.fn(),
  useRefreshAccount: vi.fn(),
  useConfig: vi.fn(),
  useRepos: vi.fn(),
  useBranches: vi.fn(),
}))

const NOOP_MUTATION = {
  mutate: vi.fn(),
  isPending: false,
}

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

describe('SessionActions (via Dashboard)', () => {
  beforeEach(() => {
    vi.mocked(hooks.useAccounts).mockReturnValue({
      data: [],
      isLoading: false,
    } as ReturnType<typeof hooks.useAccounts>)

    vi.mocked(hooks.useSessions).mockReturnValue({
      data: [
        {
          id: 'sess-1',
          name: 'Alpha session',
          accountName: 'alice',
          status: 'working' as const,
          createdAt: '2026-06-19T10:00:00Z',
          updatedAt: '2026-06-19T10:00:00Z',
        },
      ],
      isLoading: false,
    } as ReturnType<typeof hooks.useSessions>)

    vi.mocked(hooks.useCreateSession).mockReturnValue({
      ...NOOP_MUTATION,
      mutate: vi.fn(),
    } as unknown as ReturnType<typeof hooks.useCreateSession>)

    vi.mocked(hooks.useRenameSession).mockReturnValue({
      ...NOOP_MUTATION,
      mutate: vi.fn(),
    } as unknown as ReturnType<typeof hooks.useRenameSession>)

    vi.mocked(hooks.useCloseSession).mockReturnValue({
      ...NOOP_MUTATION,
      mutate: vi.fn(),
    } as unknown as ReturnType<typeof hooks.useCloseSession>)

    vi.mocked(hooks.useRefreshAccount).mockReturnValue({
      ...NOOP_MUTATION,
      mutate: vi.fn(),
    } as unknown as ReturnType<typeof hooks.useRefreshAccount>)

    vi.mocked(hooks.useConfig).mockReturnValue({
      data: { repoRoot: '/home/user/GitHub' },
      isLoading: false,
    } as ReturnType<typeof hooks.useConfig>)

    vi.mocked(hooks.useRepos).mockReturnValue({
      data: [],
      isLoading: false,
    } as ReturnType<typeof hooks.useRepos>)

    vi.mocked(hooks.useBranches).mockReturnValue({
      data: { current: null, branches: [] },
      isLoading: false,
    } as ReturnType<typeof hooks.useBranches>)
  })

  it('renders the "+ New" button', () => {
    render(<Dashboard />, { wrapper })
    expect(screen.getByRole('button', { name: /new session/i })).toBeInTheDocument()
  })

  it('opens a New Session dialog when "+ New" is clicked', async () => {
    const user = userEvent.setup()
    render(<Dashboard />, { wrapper })

    await user.click(screen.getByRole('button', { name: /new session/i }))

    expect(screen.getByRole('dialog', { name: /new session/i })).toBeInTheDocument()
    expect(screen.getByLabelText(/name/i)).toBeInTheDocument()
  })

  it('closes the dialog when Cancel is clicked', async () => {
    const user = userEvent.setup()
    render(<Dashboard />, { wrapper })

    await user.click(screen.getByRole('button', { name: /new session/i }))
    expect(screen.getByRole('dialog', { name: /new session/i })).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /cancel/i }))
    await waitFor(() => {
      expect(screen.queryByRole('dialog', { name: /new session/i })).not.toBeInTheDocument()
    })
  })

  it('closes the dialog when Escape is pressed', async () => {
    const user = userEvent.setup()
    render(<Dashboard />, { wrapper })

    await user.click(screen.getByRole('button', { name: /new session/i }))
    expect(screen.getByRole('dialog', { name: /new session/i })).toBeInTheDocument()

    await user.keyboard('{Escape}')
    await waitFor(() => {
      expect(screen.queryByRole('dialog', { name: /new session/i })).not.toBeInTheDocument()
    })
  })

  it('rename option is accessible via overflow menu', async () => {
    const user = userEvent.setup()
    render(<Dashboard />, { wrapper })

    await user.click(screen.getByRole('button', { name: /actions for session alpha session/i }))
    expect(screen.getByRole('menuitem', { name: /rename/i })).toBeInTheDocument()
  })

  it('close option is accessible via overflow menu', async () => {
    const user = userEvent.setup()
    render(<Dashboard />, { wrapper })

    await user.click(screen.getByRole('button', { name: /actions for session alpha session/i }))
    expect(screen.getByRole('menuitem', { name: /close/i })).toBeInTheDocument()
  })

  it('opening rename shows a rename dialog', async () => {
    const user = userEvent.setup()
    render(<Dashboard />, { wrapper })

    await user.click(screen.getByRole('button', { name: /actions for session alpha session/i }))
    await user.click(screen.getByRole('menuitem', { name: /rename/i }))

    expect(screen.getByRole('dialog', { name: /rename session/i })).toBeInTheDocument()
  })
})
