import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { SessionList } from '../SessionList'
import type { Session, Account } from '@/api/types'

const SESSIONS: Session[] = [
  {
    id: 'sess-1',
    name: 'Work session',
    accountName: 'alice',
    status: 'working',
    createdAt: '2026-06-19T10:00:00Z',
    updatedAt: '2026-06-19T10:00:00Z',
  },
  {
    id: 'sess-2',
    name: 'Research',
    accountName: 'bob',
    status: 'idle',
    createdAt: '2026-06-19T11:00:00Z',
    updatedAt: '2026-06-19T11:00:00Z',
  },
]

const ACCOUNTS: Account[] = [
  {
    name: 'alice',
    status: 'ok',
    usage: { session: { pct: 30, resetsAt: null }, weekly: { pct: 50, resetsAt: null } },
  },
  {
    name: 'bob',
    status: 'limited',
    usage: { session: { pct: 95, resetsAt: null }, weekly: { pct: 80, resetsAt: null } },
  },
]

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

describe('SessionList', () => {
  const onNew = vi.fn()
  const onOpen = vi.fn()
  const onRename = vi.fn()
  const onClose = vi.fn()

  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders a row for each session showing name and account', () => {
    render(
      <SessionList
        sessions={SESSIONS}
        accounts={ACCOUNTS}
        onNew={onNew}
        onOpen={onOpen}
        onRename={onRename}
        onClose={onClose}
      />,
      { wrapper },
    )

    expect(screen.getByText('Work session')).toBeInTheDocument()
    expect(screen.getByText('Research')).toBeInTheDocument()
    expect(screen.getByText('alice')).toBeInTheDocument()
    expect(screen.getByText('bob')).toBeInTheDocument()
  })

  it('renders status text (not just color) for each session', () => {
    render(
      <SessionList
        sessions={SESSIONS}
        accounts={ACCOUNTS}
        onNew={onNew}
        onOpen={onOpen}
        onRename={onRename}
        onClose={onClose}
      />,
      { wrapper },
    )

    expect(screen.getByText('Working')).toBeInTheDocument()
    expect(screen.getByText('Idle')).toBeInTheDocument()
  })

  it('calls onOpen when the session main button is clicked', async () => {
    const user = userEvent.setup()
    render(
      <SessionList
        sessions={SESSIONS}
        accounts={ACCOUNTS}
        onNew={onNew}
        onOpen={onOpen}
        onRename={onRename}
        onClose={onClose}
      />,
      { wrapper },
    )

    await user.click(screen.getByRole('button', { name: /open session work session/i }))
    expect(onOpen).toHaveBeenCalledWith('sess-1')
  })

  it('calls onOpen when Enter is pressed on the session main button', async () => {
    const user = userEvent.setup()
    render(
      <SessionList
        sessions={SESSIONS}
        accounts={ACCOUNTS}
        onNew={onNew}
        onOpen={onOpen}
        onRename={onRename}
        onClose={onClose}
      />,
      { wrapper },
    )

    const openButton = screen.getByRole('button', { name: /open session work session/i })
    openButton.focus()
    await user.keyboard('{Enter}')
    expect(onOpen).toHaveBeenCalledWith('sess-1')
  })

  it('shows EmptyState when sessions list is empty', () => {
    render(
      <SessionList
        sessions={[]}
        accounts={ACCOUNTS}
        onNew={onNew}
        onOpen={onOpen}
        onRename={onRename}
        onClose={onClose}
      />,
      { wrapper },
    )

    expect(screen.getByText(/no sessions/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /new session/i })).toBeInTheDocument()
  })

  it('shows loading skeletons when isLoading is true', () => {
    render(
      <SessionList
        sessions={[]}
        accounts={[]}
        isLoading={true}
        onNew={onNew}
        onOpen={onOpen}
        onRename={onRename}
        onClose={onClose}
      />,
      { wrapper },
    )

    // Loading container has aria-busy
    expect(screen.getByLabelText('Loading sessions')).toBeInTheDocument()
  })

  it('shows rename and close options in overflow menu', async () => {
    const user = userEvent.setup()
    render(
      <SessionList
        sessions={[SESSIONS[0]]}
        accounts={ACCOUNTS}
        onNew={onNew}
        onOpen={onOpen}
        onRename={onRename}
        onClose={onClose}
      />,
      { wrapper },
    )

    // Open the overflow menu for the first session
    await user.click(screen.getByRole('button', { name: /actions for session work session/i }))

    expect(screen.getByRole('menuitem', { name: /rename/i })).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: /close/i })).toBeInTheDocument()
  })

  it('calls onRename when rename is selected from overflow menu', async () => {
    const user = userEvent.setup()
    render(
      <SessionList
        sessions={[SESSIONS[0]]}
        accounts={ACCOUNTS}
        onNew={onNew}
        onOpen={onOpen}
        onRename={onRename}
        onClose={onClose}
      />,
      { wrapper },
    )

    await user.click(screen.getByRole('button', { name: /actions for session work session/i }))
    await user.click(screen.getByRole('menuitem', { name: /rename/i }))
    expect(onRename).toHaveBeenCalledWith('sess-1')
  })

  it('calls onClose when close is selected from overflow menu', async () => {
    const user = userEvent.setup()
    render(
      <SessionList
        sessions={[SESSIONS[0]]}
        accounts={ACCOUNTS}
        onNew={onNew}
        onOpen={onOpen}
        onRename={onRename}
        onClose={onClose}
      />,
      { wrapper },
    )

    await user.click(screen.getByRole('button', { name: /actions for session work session/i }))
    await user.click(screen.getByRole('menuitem', { name: /close/i }))
    expect(onClose).toHaveBeenCalledWith('sess-1')
  })
})

describe('SessionCard — null usage guard (needs-reauth account)', () => {
  // Regression: before the fix, a session whose account has usage:null
  // crashed SessionCard with "Cannot read properties of null (reading 'session')".

  const onNew = vi.fn()
  const onOpen = vi.fn()
  const onRename = vi.fn()
  const onClose = vi.fn()

  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders without throwing when account.usage is null', () => {
    const nullUsageAccount: Account = {
      name: 'kindash',
      status: 'needs-relogin',
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      usage: null as any,
      nickname: null,
    }
    const session: Session = {
      id: 'sess-nr',
      name: 'Needs Reauth Session',
      accountName: 'kindash',
      status: 'idle',
      createdAt: '2026-06-20T00:00:00Z',
      updatedAt: '2026-06-20T00:00:00Z',
    }

    expect(() =>
      render(
        <SessionList
          sessions={[session]}
          accounts={[nullUsageAccount]}
          onNew={onNew}
          onOpen={onOpen}
          onRename={onRename}
          onClose={onClose}
        />,
        { wrapper },
      ),
    ).not.toThrow()
  })

  it('does not render UsageBadge text when account.usage is null', () => {
    const nullUsageAccount: Account = {
      name: 'kindash',
      status: 'needs-relogin',
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      usage: null as any,
      nickname: null,
    }
    const session: Session = {
      id: 'sess-nr',
      name: 'Needs Reauth Session',
      accountName: 'kindash',
      status: 'idle',
      createdAt: '2026-06-20T00:00:00Z',
      updatedAt: '2026-06-20T00:00:00Z',
    }

    render(
      <SessionList
        sessions={[session]}
        accounts={[nullUsageAccount]}
        onNew={onNew}
        onOpen={onOpen}
        onRename={onRename}
        onClose={onClose}
      />,
      { wrapper },
    )

    // UsageBadge renders with aria-label "session usage: N%" / "weekly usage: N%".
    // Neither should be present when account.usage is null.
    expect(screen.queryByLabelText(/session usage:/i)).not.toBeInTheDocument()
    expect(screen.queryByLabelText(/weekly usage:/i)).not.toBeInTheDocument()
    // Session name still renders
    expect(screen.getByText('Needs Reauth Session')).toBeInTheDocument()
  })
})
