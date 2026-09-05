import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { SessionList } from '../SessionList'
import type { Session, Account } from '@/api/types'
import { anthropicUsage, account, session } from '@/test/factories'

const SESSIONS: Session[] = [
  session({
    id: 'sess-1',
    name: 'Work session',
    accountName: 'alice',
    status: 'in_progress',
    createdAt: '2026-06-19T10:00:00Z',
    updatedAt: '2026-06-19T10:00:00Z',
  }),
  session({
    id: 'sess-2',
    name: 'Research',
    accountName: 'bob',
    status: 'idle',
    createdAt: '2026-06-19T11:00:00Z',
    updatedAt: '2026-06-19T11:00:00Z',
  }),
]

const ACCOUNTS: Account[] = [
  account({
    name: 'alice',
    status: 'ok',
    usage: anthropicUsage(30, 50),
  }),
  account({
    name: 'bob',
    status: 'limited',
    usage: anthropicUsage(95, 80),
  }),
]

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

const baseProps = {
  sessions: SESSIONS,
  accounts: ACCOUNTS,
  onNew: vi.fn(),
  onOpen: vi.fn(),
  onRename: vi.fn(),
  onClose: vi.fn(),
  onDelete: vi.fn(),
  onQuickResume: vi.fn(),
}

describe('SessionList', () => {
  const onNew = vi.fn()
  const onOpen = vi.fn()
  const onRename = vi.fn()
  const onClose = vi.fn()
  const onDelete = vi.fn()
  const onQuickResume = vi.fn()

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
        onQuickResume={onQuickResume}
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
        onQuickResume={onQuickResume}
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
        onQuickResume={onQuickResume}
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
        onQuickResume={onQuickResume}
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
        onQuickResume={onQuickResume}
      />,
      { wrapper },
    )
    const selected = screen.getByLabelText(/open session research/i)
    expect(selected).toHaveAttribute('data-selected', 'true')
    const other = screen.getByLabelText(/open session work session/i)
    expect(other).toHaveAttribute('data-selected', 'false')
  })

  // Successor to 'session container has aria-activedescendant when a session is
  // selected'. That assertion had two halves and both are still pinned, just in
  // their correct homes:
  //   · the container is the DispatchBar's aria-controls target → asserted here;
  //   · the active row is announced as `row-<id>` → asserted on the DispatchBar
  //     textarea, where the attribute is valid, by KeyboardSessions.test.tsx
  //     ('dispatch textarea has aria-activedescendant pointing at the selected
  //     row') and DesktopShell.test.tsx.
  // The copy on this div was invalid ARIA (aria-activedescendant on a role-less
  // element) and is now asserted absent so it cannot come back.
  it('session container is the aria-controls target and carries no aria-activedescendant of its own', () => {
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
        onQuickResume={onQuickResume}
      />,
      { wrapper },
    )
    const container = document.getElementById('session-listbox')!
    expect(container).toBeTruthy()
    expect(container.hasAttribute('aria-activedescendant')).toBe(false)
    // The selected row is still identifiable by the id the textarea points at.
    expect(document.getElementById('row-sess-1')).toBeTruthy()
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
        onQuickResume={onQuickResume}
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
        onQuickResume={onQuickResume}
      />,
      { wrapper },
    )

    // Loading container has aria-busy
    expect(screen.getByLabelText('Loading sessions')).toBeInTheDocument()
  })

  it('offers rename, archive, and delete in each session’s ⋯ menu', async () => {
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
        onQuickResume={onQuickResume}
      />,
      { wrapper },
    )

    await user.click(screen.getByRole('button', { name: /more actions for session work session/i }))
    expect(screen.getByRole('menuitem', { name: /rename/i })).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: /archive/i })).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: /delete/i })).toBeInTheDocument()
  })

  it('calls onRename when the rename menu item is clicked', async () => {
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
        onQuickResume={onQuickResume}
      />,
      { wrapper },
    )

    await user.click(screen.getByRole('button', { name: /more actions for session work session/i }))
    await user.click(screen.getByRole('menuitem', { name: /rename/i }))
    expect(onRename).toHaveBeenCalledWith('sess-1')
  })

  it('calls onClose when the archive menu item is clicked', async () => {
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
        onQuickResume={onQuickResume}
      />,
      { wrapper },
    )

    await user.click(screen.getByRole('button', { name: /more actions for session work session/i }))
    await user.click(screen.getByRole('menuitem', { name: /archive/i }))
    expect(onClose).toHaveBeenCalledWith('sess-1')
  })

  it('calls onDelete when the delete menu item is clicked', async () => {
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
        onQuickResume={onQuickResume}
      />,
      { wrapper },
    )

    await user.click(screen.getByRole('button', { name: /more actions for session work session/i }))
    await user.click(screen.getByRole('menuitem', { name: /delete/i }))
    expect(onDelete).toHaveBeenCalledWith('sess-1')
  })

  it('renders group headers with counts when groups provided', () => {
    const groups = [
      { key: 'needs-you', label: 'Needs you', count: 1, sessions: [SESSIONS[0]] },
      { key: 'your-turn', label: 'Your turn', count: 1, sessions: [SESSIONS[1]] },
    ]
    render(
      <SessionList
        sessions={SESSIONS}
        groups={groups}
        accounts={ACCOUNTS}
        onNew={() => {}}
        onOpen={() => {}}
        onRename={() => {}}
        onClose={() => {}}
        onDelete={() => {}}
        onQuickResume={() => {}}
      />,
      { wrapper },
    )
    expect(screen.getByText('Needs you')).toBeInTheDocument()
    expect(screen.getByText('Your turn')).toBeInTheDocument()
    // both rows still render in flat order
    expect(screen.getByText('Work session')).toBeInTheDocument()
    expect(screen.getByText('Research')).toBeInTheDocument()
  })

  it('keeps group headers visible when every group is collapsed', () => {
    // Regression: in the real app SessionList is fed sessions={orderedSessions},
    // and useSessionOrdering excludes collapsed groups from orderedSessions. So
    // collapsing every group makes the `sessions` prop empty while `groups` is
    // still full. The empty-state guard must NOT swallow the headers, otherwise
    // it looks like all sessions vanished.
    const groups = [
      { key: 'busy', label: 'Working', count: 1, sessions: [SESSIONS[0]] },
      { key: 'your-turn', label: 'Done', count: 1, sessions: [SESSIONS[1]] },
    ]
    render(
      <SessionList
        {...baseProps}
        sessions={[]} // orderedSessions when everything is collapsed
        groups={groups}
        collapsedKeys={new Set(['busy', 'your-turn'])}
        onToggleCollapse={vi.fn()}
      />,
      { wrapper },
    )
    // Both headers (and their counts) are still shown so the user can re-expand.
    expect(screen.getByRole('button', { name: /Working/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Done/i })).toBeInTheDocument()
    // Not the "no sessions" empty state.
    expect(screen.queryByRole('button', { name: /new session/i })).not.toBeInTheDocument()
  })

  it('renders no header for a null-label group', () => {
    const groups = [{ key: 'all', label: null, count: SESSIONS.length, sessions: SESSIONS }]
    render(
      <SessionList
        sessions={SESSIONS}
        groups={groups}
        accounts={ACCOUNTS}
        onNew={() => {}}
        onOpen={() => {}}
        onRename={() => {}}
        onClose={() => {}}
        onDelete={() => {}}
        onQuickResume={() => {}}
      />,
      { wrapper },
    )
    expect(screen.queryByRole('presentation')).not.toBeInTheDocument()
    expect(screen.getByText('Work session')).toBeInTheDocument()
  })

  it('collapsing a group hides its cards but keeps header and count', async () => {
    const onToggleCollapse = vi.fn()
    const groups = [
      { key: 'busy', label: 'Working', count: 1, sessions: [SESSIONS[0]] },
      { key: 'your-turn', label: 'Done', count: 1, sessions: [SESSIONS[1]] },
    ]
    const { rerender } = render(
      <SessionList {...baseProps} groups={groups} collapsedKeys={new Set()} onToggleCollapse={onToggleCollapse} />,
      { wrapper },
    )
    // expanded: the "Working" group's card is visible
    expect(screen.getByText(SESSIONS[0].name)).toBeInTheDocument()

    const header = screen.getByRole('button', { name: /Working/i })
    expect(header).toHaveAttribute('aria-expanded', 'true')
    await userEvent.click(header)
    expect(onToggleCollapse).toHaveBeenCalledWith('busy')

    // re-render as collapsed and assert the card is gone but header + count remain
    rerender(
      <SessionList {...baseProps} groups={groups} collapsedKeys={new Set(['busy'])} onToggleCollapse={onToggleCollapse} />,
    )
    expect(screen.queryByText(SESSIONS[0].name)).not.toBeInTheDocument()
    const collapsedHeader = screen.getByRole('button', { name: /Working/i })
    expect(collapsedHeader).toHaveAttribute('aria-expanded', 'false')
    expect(within(collapsedHeader).getByText('1')).toBeInTheDocument() // count still shown inside the header
  })
})

describe('SessionList — repo label', () => {
  const onNew = vi.fn()
  const onOpen = vi.fn()
  const onRename = vi.fn()
  const onClose = vi.fn()
  const onDelete = vi.fn()
  const onQuickResume = vi.fn()

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
        onQuickResume={onQuickResume}
      />,
      { wrapper },
    )

    expect(screen.getByText('acme/widget')).toBeInTheDocument()
    expect(screen.getByText('Scratch')).toBeInTheDocument()
  })
})

describe('SessionList — pure renderer (no internal sort)', () => {
  const onNew = vi.fn()
  const onOpen = vi.fn()
  const onRename = vi.fn()
  const onClose = vi.fn()
  const onDelete = vi.fn()
  const onQuickResume = vi.fn()

  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders cards in the same order as the sessions prop (pre-sorted by caller)', () => {
    // Pass sessions in a specific order: busy → your-turn → needs-you
    // SessionList should render them exactly as given, not re-sort.
    const sessions: Session[] = [
      session({
        id: 'a',
        name: 'A busy',
        accountName: 'alice',
        status: 'in_progress',
        createdAt: '2026-06-19T10:00:00Z',
        updatedAt: '2026-06-19T10:00:00Z',
      }),
      session({
        id: 'b',
        name: 'B done',
        accountName: 'alice',
        status: 'done',
        createdAt: '2026-06-19T10:00:00Z',
        updatedAt: '2026-06-19T10:00:00Z',
      }),
      session({
        id: 'c',
        name: 'C blocked',
        accountName: 'alice',
        status: 'waiting_input',
        createdAt: '2026-06-19T10:00:00Z',
        updatedAt: '2026-06-19T10:00:00Z',
      }),
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
        onQuickResume={onQuickResume}
      />,
      { wrapper },
    )
    const order = screen.getAllByRole('button', { name: /Open session/ }).map(
      (b) => b.getAttribute('aria-label'),
    )
    // Must match input order exactly — SessionList is now a pure renderer.
    expect(order).toEqual([
      'Open session A busy',
      'Open session B done',
      'Open session C blocked',
    ])
  })
})

describe('SessionList — group header keyboard nav', () => {
  const mkSession = (id: string): Session =>
    session({
      id,
      name: `Session ${id}`,
      accountName: 'alice',
      status: 'idle',
      createdAt: '2026-06-24T10:00:00Z',
      updatedAt: '2026-06-24T10:00:00Z',
    })

  it('renders a highlighted group header and routes its keys', async () => {
    const u = userEvent.setup()
    const onExpand = vi.fn()
    const groups = [
      { key: 'busy', label: 'Working', count: 1, sessions: [mkSession('s1')] },
    ]
    render(
      <SessionList
        sessions={groups.flatMap((g) => g.sessions)}
        groups={groups}
        accounts={[]}
        listMode
        collapsedKeys={new Set(['busy'])}
        selectedGroupKey="busy"
        onToggleCollapse={vi.fn()}
        onExpand={onExpand}
        onCollapse={vi.fn()}
        onNavigate={vi.fn()}
        onExit={vi.fn()}
        onNew={vi.fn()} onOpen={vi.fn()} onRename={vi.fn()}
        onClose={vi.fn()} onDelete={vi.fn()} onQuickResume={vi.fn()}
      />,
      { wrapper },
    )
    const header = screen.getByRole('button', { name: /Working/ })
    expect(header).toHaveAttribute('id', 'group-header-busy')
    header.focus()
    await u.keyboard('{ArrowRight}')
    expect(onExpand).toHaveBeenCalledWith('busy')
  })
})

describe('SessionList — group panel ARIA disclosure', () => {
  const mkSession = (id: string): Session =>
    session({
      id,
      name: `Session ${id}`,
      accountName: 'alice',
      status: 'idle',
      createdAt: '2026-06-24T10:00:00Z',
      updatedAt: '2026-06-24T10:00:00Z',
    })

  it('group panel has role=group and aria-labelledby pointing at the header when group.label is non-null', () => {
    const groups = [
      { key: 'busy', label: 'Working', count: 1, sessions: [mkSession('s1')] },
    ]
    render(
      <SessionList
        sessions={groups.flatMap((g) => g.sessions)}
        groups={groups}
        accounts={[]}
        collapsedKeys={new Set()}
        onNew={vi.fn()} onOpen={vi.fn()} onRename={vi.fn()}
        onClose={vi.fn()} onDelete={vi.fn()} onQuickResume={vi.fn()}
      />,
      { wrapper },
    )
    const panel = document.getElementById('group-panel-busy')!
    expect(panel).not.toBeNull()
    expect(panel).toHaveAttribute('role', 'group')
    expect(panel).toHaveAttribute('aria-labelledby', 'group-header-busy')
  })

  it('group panel has NO role or aria-labelledby when group.label is null (ungrouped)', () => {
    const groups = [
      { key: 'all', label: null, count: 2, sessions: [mkSession('s1'), mkSession('s2')] },
    ]
    render(
      <SessionList
        sessions={groups.flatMap((g) => g.sessions)}
        groups={groups}
        accounts={[]}
        collapsedKeys={new Set()}
        onNew={vi.fn()} onOpen={vi.fn()} onRename={vi.fn()}
        onClose={vi.fn()} onDelete={vi.fn()} onQuickResume={vi.fn()}
      />,
      { wrapper },
    )
    const panel = document.getElementById('group-panel-all')!
    expect(panel).not.toBeNull()
    expect(panel).not.toHaveAttribute('role')
    expect(panel).not.toHaveAttribute('aria-labelledby')
  })
})

describe('SessionList — no-matches state', () => {
  it('shows a no-matches state (not EmptyState) when filtered to zero', async () => {
    const onClearSearch = vi.fn()
    render(
      <SessionList
        sessions={[]}
        accounts={[]}
        isFiltered
        onClearSearch={onClearSearch}
        onNew={() => {}}
        onOpen={() => {}}
        onRename={() => {}}
        onClose={() => {}}
        onDelete={() => {}}
        onQuickResume={() => {}}
      />,
      { wrapper },
    )
    // R9 successor to the `/clear search/i` assertion this replaces (Task 10).
    // Both halves are matched EXACTLY rather than by the old loose regexes: "No
    // sessions match your search." and "Clear search" are both prefixes of the
    // new strings, so /no sessions match/i and /clear search/i would have stayed
    // green whether or not the filter-aware copy ever landed.
    expect(screen.getByText('No sessions match your search or filters.')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Clear search and filters' }))
    expect(onClearSearch).toHaveBeenCalled()
  })

  it('shows EmptyState when there are zero sessions and no active filter', () => {
    render(
      <SessionList
        sessions={[]}
        accounts={[]}
        onNew={() => {}}
        onOpen={() => {}}
        onRename={() => {}}
        onClose={() => {}}
        onDelete={() => {}}
        onQuickResume={() => {}}
      />,
      { wrapper },
    )
    expect(screen.queryByText(/no sessions match/i)).toBeNull()
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
  const onQuickResume = vi.fn()

  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders without throwing when account.usage is null', () => {
    const nullUsageAccount: Account = account({
      name: 'kindash',
      status: 'needs-relogin',
      usage: null,
      nickname: null,
    })
    const nullUsageSession: Session = session({
      id: 'sess-nr',
      name: 'Needs Reauth Session',
      accountName: 'kindash',
      status: 'idle',
      createdAt: '2026-06-20T00:00:00Z',
      updatedAt: '2026-06-20T00:00:00Z',
    })

    expect(() =>
      render(
        <SessionList
          sessions={[nullUsageSession]}
          accounts={[nullUsageAccount]}
          onNew={onNew}
          onOpen={onOpen}
          onRename={onRename}
          onClose={onClose}
          onDelete={onDelete}
          onQuickResume={onQuickResume}
        />,
        { wrapper },
      ),
    ).not.toThrow()
  })

  it('does not render UsageBadge text when account.usage is null', () => {
    const nullUsageAccount: Account = account({
      name: 'kindash',
      status: 'needs-relogin',
      usage: null,
      nickname: null,
    })
    const nullUsageSession: Session = session({
      id: 'sess-nr',
      name: 'Needs Reauth Session',
      accountName: 'kindash',
      status: 'idle',
      createdAt: '2026-06-20T00:00:00Z',
      updatedAt: '2026-06-20T00:00:00Z',
    })

    render(
      <SessionList
        sessions={[nullUsageSession]}
        accounts={[nullUsageAccount]}
        onNew={onNew}
        onOpen={onOpen}
        onRename={onRename}
        onClose={onClose}
        onDelete={onDelete}
        onQuickResume={onQuickResume}
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

describe('SessionList — Resume interrupted button', () => {
  const sharedProps = {
    accounts: ACCOUNTS,
    onNew: vi.fn(),
    onOpen: vi.fn(),
    onRename: vi.fn(),
    onClose: vi.fn(),
    onDelete: vi.fn(),
    onQuickResume: vi.fn(),
  }

  beforeEach(() => vi.clearAllMocks())

  it('renders "Resume interrupted (2)" when two sessions have resumable:true', () => {
    const sessions: Session[] = [
      { ...SESSIONS[0], resumable: true },
      { ...SESSIONS[1], resumable: true },
      session({
        id: 'sess-3',
        name: 'Not resumable',
        accountName: 'alice',
        status: 'idle',
        createdAt: '2026-06-19T12:00:00Z',
        updatedAt: '2026-06-19T12:00:00Z',
        resumable: false,
      }),
    ]
    render(<SessionList {...sharedProps} sessions={sessions} />, { wrapper })
    expect(screen.getByRole('button', { name: /resume interrupted \(2\)/i })).toBeInTheDocument()
  })

  it('does not render the Resume interrupted button when no sessions are resumable', () => {
    const sessions: Session[] = [
      { ...SESSIONS[0], resumable: false },
      { ...SESSIONS[1] },
    ]
    render(<SessionList {...sharedProps} sessions={sessions} />, { wrapper })
    expect(screen.queryByRole('button', { name: /resume interrupted/i })).not.toBeInTheDocument()
  })
})

describe('SessionList — Rotate stalled button', () => {
  const sharedProps = {
    accounts: ACCOUNTS, onNew: vi.fn(), onOpen: vi.fn(), onRename: vi.fn(),
    onClose: vi.fn(), onDelete: vi.fn(), onQuickResume: vi.fn(),
  }
  beforeEach(() => vi.clearAllMocks())

  it('renders "Rotate stalled (2)" when two sessions are usageStalled', () => {
    const sessions: Session[] = [
      { ...SESSIONS[0], status: 'needs_attention', usageStalled: true },
      { ...SESSIONS[1], status: 'limited', usageStalled: true },
      session({ id: 'sess-3', name: 'Healthy', accountName: 'alice', status: 'idle',
                createdAt: '2026-06-19T12:00:00Z', updatedAt: '2026-06-19T12:00:00Z' }),
    ]
    render(<SessionList {...sharedProps} sessions={sessions} />, { wrapper })
    expect(screen.getByRole('button', { name: /rotate stalled \(2\)/i })).toBeInTheDocument()
  })

  it('does not render the button when nothing is stalled', () => {
    render(<SessionList {...sharedProps} sessions={SESSIONS} />, { wrapper })
    expect(screen.queryByRole('button', { name: /rotate stalled/i })).not.toBeInTheDocument()
  })
})
