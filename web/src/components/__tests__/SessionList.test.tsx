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
    status: 'in_progress',
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
  const onDelete = vi.fn()

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
        onDelete={onDelete}
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
        onDelete={onDelete}
      />,
      { wrapper },
    )

    expect(screen.getByText('Working…')).toBeInTheDocument()
    expect(screen.getByText('Ready')).toBeInTheDocument()
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
        onDelete={onDelete}
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
        onDelete={onDelete}
      />,
      { wrapper },
    )

    const openButton = screen.getByRole('button', { name: /open session work session/i })
    openButton.focus()
    await user.keyboard('{Enter}')
    expect(onOpen).toHaveBeenCalledWith('sess-1')
  })

  it('marks the selected session row with data-selected', () => {
    render(
      <SessionList
        sessions={SESSIONS}
        accounts={ACCOUNTS}
        selectedId="sess-2"
        onNew={onNew}
        onOpen={onOpen}
        onRename={onRename}
        onClose={onClose}
        onDelete={onDelete}
      />,
      { wrapper },
    )
    const selected = screen.getByLabelText(/open session research/i)
    expect(selected).toHaveAttribute('data-selected', 'true')
    const other = screen.getByLabelText(/open session work session/i)
    expect(other).toHaveAttribute('data-selected', 'false')
  })

  it('session container has aria-activedescendant when a session is selected', () => {
    render(
      <SessionList
        sessions={SESSIONS}
        accounts={ACCOUNTS}
        selectedId="sess-1"
        onNew={onNew}
        onOpen={onOpen}
        onRename={onRename}
        onClose={onClose}
        onDelete={onDelete}
      />,
      { wrapper },
    )
    const container = document.getElementById('session-listbox')!
    expect(container).toHaveAttribute('aria-activedescendant', 'row-sess-1')
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
        onDelete={onDelete}
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
        onDelete={onDelete}
      />,
      { wrapper },
    )

    // Loading container has aria-busy
    expect(screen.getByLabelText('Loading sessions')).toBeInTheDocument()
  })

  it('shows rename, archive, and delete buttons on each session', () => {
    render(
      <SessionList
        sessions={[SESSIONS[0]]}
        accounts={ACCOUNTS}
        onNew={onNew}
        onOpen={onOpen}
        onRename={onRename}
        onClose={onClose}
        onDelete={onDelete}
      />,
      { wrapper },
    )

    expect(screen.getByRole('button', { name: /rename session work session/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /archive session work session/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /delete session work session/i })).toBeInTheDocument()
  })

  it('calls onRename when the rename button is clicked', async () => {
    const user = userEvent.setup()
    render(
      <SessionList
        sessions={[SESSIONS[0]]}
        accounts={ACCOUNTS}
        onNew={onNew}
        onOpen={onOpen}
        onRename={onRename}
        onClose={onClose}
        onDelete={onDelete}
      />,
      { wrapper },
    )

    await user.click(screen.getByRole('button', { name: /rename session work session/i }))
    expect(onRename).toHaveBeenCalledWith('sess-1')
  })

  it('calls onClose when the archive button is clicked', async () => {
    const user = userEvent.setup()
    render(
      <SessionList
        sessions={[SESSIONS[0]]}
        accounts={ACCOUNTS}
        onNew={onNew}
        onOpen={onOpen}
        onRename={onRename}
        onClose={onClose}
        onDelete={onDelete}
      />,
      { wrapper },
    )

    await user.click(screen.getByRole('button', { name: /archive session work session/i }))
    expect(onClose).toHaveBeenCalledWith('sess-1')
  })

  it('calls onDelete when the delete button is clicked', async () => {
    const user = userEvent.setup()
    render(
      <SessionList
        sessions={[SESSIONS[0]]}
        accounts={ACCOUNTS}
        onNew={onNew}
        onOpen={onOpen}
        onRename={onRename}
        onClose={onClose}
        onDelete={onDelete}
      />,
      { wrapper },
    )

    await user.click(screen.getByRole('button', { name: /delete session work session/i }))
    expect(onDelete).toHaveBeenCalledWith('sess-1')
  })
})

describe('SessionList — repo label', () => {
  const onNew = vi.fn()
  const onOpen = vi.fn()
  const onRename = vi.fn()
  const onClose = vi.fn()
  const onDelete = vi.fn()

  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('shows which repo each session is working on, and "Scratch" for repo-less ones', () => {
    const sessions: Session[] = [
      { ...SESSIONS[0], repo: 'acme/widget' },
      { ...SESSIONS[1], repo: null },
    ]
    render(
      <SessionList
        sessions={sessions}
        accounts={ACCOUNTS}
        onNew={onNew}
        onOpen={onOpen}
        onRename={onRename}
        onClose={onClose}
        onDelete={onDelete}
      />,
      { wrapper },
    )

    expect(screen.getByText('acme/widget')).toBeInTheDocument()
    expect(screen.getByText('Scratch')).toBeInTheDocument()
  })
})

describe('SessionList — tier sort', () => {
  const onNew = vi.fn()
  const onOpen = vi.fn()
  const onRename = vi.fn()
  const onClose = vi.fn()
  const onDelete = vi.fn()

  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('sorts needs-you and error to the top, busy to the bottom, stable within tier', () => {
    const sessions: Session[] = [
      {
        id: 'a',
        name: 'A busy',
        accountName: 'alice',
        status: 'in_progress',
        createdAt: '2026-06-19T10:00:00Z',
        updatedAt: '2026-06-19T10:00:00Z',
      },
      {
        id: 'b',
        name: 'B done',
        accountName: 'alice',
        status: 'done',
        createdAt: '2026-06-19T10:00:00Z',
        updatedAt: '2026-06-19T10:00:00Z',
      },
      {
        id: 'c',
        name: 'C blocked',
        accountName: 'alice',
        status: 'waiting_input',
        createdAt: '2026-06-19T10:00:00Z',
        updatedAt: '2026-06-19T10:00:00Z',
      },
      {
        id: 'd',
        name: 'D busy2',
        accountName: 'alice',
        status: 'in_progress',
        createdAt: '2026-06-19T10:00:00Z',
        updatedAt: '2026-06-19T10:00:00Z',
      },
      {
        id: 'e',
        name: 'E error',
        accountName: 'alice',
        status: 'needs_attention',
        createdAt: '2026-06-19T10:00:00Z',
        updatedAt: '2026-06-19T10:00:00Z',
      },
    ]
    render(
      <SessionList
        sessions={sessions}
        accounts={[]}
        onNew={onNew}
        onOpen={onOpen}
        onRename={onRename}
        onClose={onClose}
        onDelete={onDelete}
      />,
      { wrapper },
    )
    const order = screen.getAllByRole('button', { name: /Open session/ }).map(
      (b) => b.getAttribute('aria-label'),
    )
    // needs-you (C) + error (E) first (original relative order C before E),
    // then your-turn (B), then busy (A before D).
    expect(order).toEqual([
      'Open session C blocked',
      'Open session E error',
      'Open session B done',
      'Open session A busy',
      'Open session D busy2',
    ])
  })
})

describe('SessionCard — null usage guard (needs-reauth account)', () => {
  // Regression: before the fix, a session whose account has usage:null
  // crashed SessionCard with "Cannot read properties of null (reading 'session')".

  const onNew = vi.fn()
  const onOpen = vi.fn()
  const onRename = vi.fn()
  const onClose = vi.fn()
  const onDelete = vi.fn()

  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders without throwing when account.usage is null', () => {
    const nullUsageAccount: Account = {
      name: 'kindash',
      status: 'needs-relogin',
      usage: null,
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
          onDelete={onDelete}
        />,
        { wrapper },
      ),
    ).not.toThrow()
  })

  it('does not render UsageBadge text when account.usage is null', () => {
    const nullUsageAccount: Account = {
      name: 'kindash',
      status: 'needs-relogin',
      usage: null,
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
        onDelete={onDelete}
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
