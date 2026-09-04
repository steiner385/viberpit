// SessionListColumn — the union of the two shells' session-list markup.
//
// Dashboard (phone) and DesktopShell (wide) each rendered their own copy of this
// column and the copies had drifted. These tests pin the four resolved
// disagreements, so a future edit cannot quietly reinstate either half-version:
//
//   1. PendingApprovalSection — phone had it, desktop did not (rows invisible).
//   2. DaemonHealthIndicator  — phone had it, desktop did not.
//   3. the held-out partition — `isPreSpawnStatus` (phone), not the narrower
//      `status === 'queued'` (desktop). DEC-3.
//   4. SessionList selectedId — the open session wins over the keyboard
//      highlight, falling back to the highlight (desktop's form).
//
// Scope note on axe: jsdom applies no stylesheet, so axe here covers roles,
// names and ARIA wiring ONLY. Its `color-contrast` rule always returns
// `incomplete` and `toHaveNoViolations()` inspects `violations` alone — a green
// assertion below is NOT evidence of adequate contrast. Contrast is checked
// separately by src/__tests__/contrast.test.ts, which does the WCAG maths on the
// tokens in index.css.
import * as React from 'react'
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { axe, toHaveNoViolations } from 'jest-axe'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { Account, DaemonHealth, Session } from '@/api/types'

expect.extend(toHaveNoViolations)

// Partial mock: only the daemon-health query is replaced, because the indicator
// renders nothing at all without a payload and "desktop was missing it" is one
// of the regressions under test. Every other hook stays real.
const daemonHealthData = vi.fn<() => DaemonHealth | undefined>()
vi.mock('@/api/hooks', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/hooks')>()
  return { ...actual, useDaemonHealth: () => ({ data: daemonHealthData() }) }
})

import { SessionListColumn, type SessionListColumnProps } from './SessionListColumn'
import { useSessionListState } from './useSessionListState'

// ── typed factories (R10: real values, never `as unknown as T`) ───────────────

function session(over: Partial<Session> = {}): Session {
  return {
    id: 'sess-1',
    name: 'First',
    accountName: 'alice',
    status: 'idle',
    createdAt: '2026-06-19T10:00:00Z',
    prs: [],
    ...over,
  }
}

function account(over: Partial<Account> = {}): Account {
  return {
    name: 'alice',
    status: 'ok',
    usage: { session: { pct: 10, resetsAt: null }, weekly: { pct: 20, resetsAt: null } },
    liveSessions: { active: 0, idle: 0 },
    rank: 1,
    rankReason: null,
    ...over,
  }
}

function daemonHealth(over: Partial<DaemonHealth> = {}): DaemonHealth {
  return {
    available: true,
    epoch: 'abc-1-deadbeef',
    startedAt: '2026-06-19T09:00:00Z',
    uptimeSec: 3600,
    restartCount24h: 0,
    lastRestartAt: null,
    lastSeenAgoSec: 1,
    ...over,
  }
}

const ACCOUNTS = [account()]
const noop = () => {}

// ── harness ──────────────────────────────────────────────────────────────────

type HarnessProps = {
  sessions: Session[]
  /** Session id to place the keyboard cursor on once the list has rendered. */
  highlight?: string
} & Partial<Omit<SessionListColumnProps, 'state' | 'accounts'>>

/**
 * Mirrors how the shell will mount the column: one `useSessionListState()` above
 * it, whose handles the shell also reads for the prompt bar. Tests therefore run
 * against the real search/ordering/navigation hooks, not stand-ins — which is
 * what makes "pre-spawn rows never reach SessionList" a real assertion.
 */
function Harness({ sessions, highlight, ...props }: HarnessProps) {
  const state = useSessionListState(sessions)
  const { highlightSession } = state.nav
  React.useEffect(() => {
    if (highlight) highlightSession(highlight)
  }, [highlight, highlightSession])
  return (
    <SessionListColumn
      state={state}
      accounts={ACCOUNTS}
      selectedId={null}
      onNew={noop}
      onOpen={noop}
      onRename={noop}
      onClose={noop}
      onDelete={noop}
      onQuickResume={noop}
      {...props}
    />
  )
}

function renderColumn(props: HarnessProps) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <Harness {...props} />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  localStorage.clear()
  daemonHealthData.mockReturnValue(daemonHealth())
})

// ── tests ────────────────────────────────────────────────────────────────────

describe('SessionListColumn — sections both shells should have had', () => {
  it('renders PendingApprovalSection for a parked session (DesktopShell had none)', () => {
    renderColumn({
      sessions: [
        session({ id: 'sess-1', name: 'First' }),
        session({ id: 'sess-2', name: 'Parked', status: 'pending_approval' }),
      ],
    })
    expect(screen.getByRole('button', { name: /awaiting approval \(1\)/i })).toBeTruthy()
    expect(screen.getByText('Parked')).toBeTruthy()
  })

  it('renders DaemonHealthIndicator (DesktopShell had none)', () => {
    daemonHealthData.mockReturnValue(daemonHealth({ uptimeSec: 7200 }))
    renderColumn({ sessions: [session()] })
    expect(screen.getByTestId('daemon-health')).toBeTruthy()
  })

  it('omits the daemon indicator when there is no daemon to watch', () => {
    daemonHealthData.mockReturnValue(daemonHealth({ available: false }))
    renderColumn({ sessions: [session()] })
    expect(screen.queryByTestId('daemon-health')).toBeNull()
  })
})

describe('SessionListColumn — DEC-3: the held-out set is isPreSpawnStatus', () => {
  it("holds a 'queued' session out of the main list and shows it in QueuedSection", () => {
    renderColumn({
      sessions: [
        session({ id: 'sess-1', name: 'Live' }),
        session({ id: 'sess-2', name: 'Waiting', status: 'queued', queuePosition: 1 }),
      ],
    })
    expect(screen.getByRole('button', { name: /queued \(1\)/i })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Open session Live' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Open session Waiting' })).toBeNull()
  })

  it("holds 'pending_approval' out too — pre-spawn but NOT literally 'queued'", () => {
    // This is the case DesktopShell's `status !== 'queued'` filter got wrong: the
    // row stayed in the grouped list, where 'pending_approval' has no status-tier
    // mapping and fell through into the your-turn bucket. Only `isPreSpawnStatus`
    // covers it, so this test is what proves DEC-3 actually took effect.
    renderColumn({
      sessions: [
        session({ id: 'sess-1', name: 'Live' }),
        session({ id: 'sess-2', name: 'Parked', status: 'pending_approval' }),
      ],
    })
    expect(screen.getByRole('button', { name: 'Open session Live' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Open session Parked' })).toBeNull()
    // …and it is not swept into the queue section either; it has its own.
    expect(screen.queryByRole('button', { name: /^queued \(/i })).toBeNull()
    expect(screen.getByRole('button', { name: /awaiting approval \(1\)/i })).toBeTruthy()
  })
})

describe('SessionListColumn — selection cursor', () => {
  it('prefers the open session over the keyboard highlight', async () => {
    renderColumn({
      sessions: [session({ id: 'sess-1', name: 'First' }), session({ id: 'sess-2', name: 'Second' })],
      selectedId: 'sess-2',
      highlight: 'sess-1',
    })
    // Dashboard passed `highlightedSession?.id ?? null`, so opening a session left
    // the cursor on whatever the keyboard had last touched. Desktop's form wins.
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Open session Second' })).toHaveAttribute(
        'data-selected',
        'true',
      ),
    )
    expect(screen.getByRole('button', { name: 'Open session First' })).toHaveAttribute(
      'data-selected',
      'false',
    )
  })

  it('falls back to the keyboard highlight when no session is open', async () => {
    renderColumn({
      sessions: [session({ id: 'sess-1', name: 'First' }), session({ id: 'sess-2', name: 'Second' })],
      selectedId: null,
      highlight: 'sess-1',
    })
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Open session First' })).toHaveAttribute(
        'data-selected',
        'true',
      ),
    )
    expect(screen.getByRole('button', { name: 'Open session Second' })).toHaveAttribute(
      'data-selected',
      'false',
    )
  })
})

describe('SessionListColumn — gates and structure', () => {
  it('hides SessionListControls when there are no sessions', () => {
    renderColumn({ sessions: [] })
    expect(screen.queryByRole('searchbox', { name: /search sessions/i })).toBeNull()
  })

  it('shows SessionListControls once a session exists', () => {
    renderColumn({ sessions: [session()] })
    expect(screen.getByRole('searchbox', { name: /search sessions/i })).toBeTruthy()
  })

  it('counts pre-spawn rows toward the controls gate', () => {
    // The gate is on the whole list, not the grouped remainder: a lone queued
    // session is still a session, and hiding search/sort there would read as a
    // broken toolbar.
    renderColumn({ sessions: [session({ status: 'queued', queuePosition: 1 })] })
    expect(screen.getByRole('searchbox', { name: /search sessions/i })).toBeTruthy()
  })

  it('mounts AdoptPanel only when adoption is enabled', () => {
    const enabled = renderColumn({ sessions: [session()], adoptionEnabled: true })
    expect(screen.getByText(/all running sessions are managed/i)).toBeTruthy()
    enabled.unmount()

    renderColumn({ sessions: [session()], adoptionEnabled: false })
    expect(screen.queryByText(/all running sessions are managed/i)).toBeNull()
  })

  it('keeps data-testid="session-list-scroll" on the scroll container', () => {
    renderColumn({ sessions: [session()] })
    const scroll = screen.getByTestId('session-list-scroll')
    // The list — and only the list — scrolls; the New Session button, the
    // controls and the daemon indicator sit outside it.
    expect(scroll.className).toContain('overflow-y-auto')
    expect(scroll.querySelector('#session-listbox')).toBeTruthy()
    expect(scroll.contains(screen.getByRole('button', { name: /new session/i }))).toBe(false)
    expect(scroll.contains(screen.getByTestId('daemon-health'))).toBe(false)
  })

  it('marks the New Session button when the new-session slot is the target', async () => {
    const onNew = vi.fn()
    const user = userEvent.setup()
    renderColumn({ sessions: [session()], newSelected: true, onNew })
    const button = screen.getByRole('button', { name: /new session/i })
    expect(button).toHaveAttribute('data-selected', 'true')
    await user.click(button)
    expect(onNew).toHaveBeenCalledTimes(1)
  })

  // Successor to "carries data-region=app.list" (Task 10 moved the marker to the
  // region that owns it): ContextList.test.tsx — "renders the session list on
  // /sessions, in a region marked app.list" asserts the marker is present there.
  // This half asserts the column does NOT also declare it, because two nested
  // [data-region="app.list"] nodes would make every region query ambiguous.
  it('leaves the app.list region marker to the shell (ContextList owns it)', () => {
    const { container } = renderColumn({ sessions: [session()] })
    expect(container.querySelector('[data-region]')).toBeNull()
  })
})

describe('SessionListColumn — the Status/Project/Environment filters (DEC-8)', () => {
  it('a Project selection narrows the rendered list', async () => {
    renderColumn({
      sessions: [
        session({ id: 'sess-1', name: 'First', repo: 'o/a' }),
        session({ id: 'sess-2', name: 'Second', repo: 'o/b' }),
      ],
    })
    expect(screen.getByRole('button', { name: 'Open session Second' })).toBeTruthy()
    await userEvent.selectOptions(screen.getByLabelText('Project'), 'o/a')
    expect(screen.getByRole('button', { name: 'Open session First' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Open session Second' })).toBeNull()
  })

  it('keeps the queued and awaiting-approval sections whole under a filter', async () => {
    // The rendering half of the decision the hook leaves open. Both pre-spawn
    // rows carry repo 'o/a'; filtering to 'o/b' is a project NEITHER belongs to,
    // so if the same predicate ran over these sections they would vanish.
    renderColumn({
      sessions: [
        session({ id: 'sess-1', name: 'First', repo: 'o/a' }),
        session({ id: 'sess-2', name: 'Second', repo: 'o/b' }),
        // NB not "Waiting": that is also a status-tier label, and the Status
        // filter now renders it as an <option>, so getByText would be ambiguous.
        session({ id: 'sess-3', name: 'InQueue', status: 'queued', queuePosition: 1, repo: 'o/a' }),
        session({ id: 'sess-4', name: 'Parked', status: 'pending_approval', repo: 'o/a' }),
      ],
    })
    await userEvent.selectOptions(screen.getByLabelText('Project'), 'o/b')
    expect(screen.queryByRole('button', { name: 'Open session First' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Open session Second' })).toBeTruthy()
    expect(screen.getByRole('button', { name: /queued \(1\)/i })).toBeTruthy()
    expect(screen.getByRole('button', { name: /awaiting approval \(1\)/i })).toBeTruthy()
    expect(screen.getByText('InQueue')).toBeTruthy()
    expect(screen.getByText('Parked')).toBeTruthy()
  })

  it('survives a zero-match filter: the controls, the selection and a way out stay', async () => {
    // 'Needs attention' is the `error` tier and no session is in it, so the list
    // goes empty by design. The strip is gated on the SESSION count, not the
    // visible rows, so the control that set the filter is still there to undo it.
    renderColumn({ sessions: [session({ id: 'sess-1', name: 'First', repo: 'o/a' })] })
    await userEvent.selectOptions(screen.getByLabelText('Status'), 'error')

    expect(screen.queryByRole('button', { name: 'Open session First' })).toBeNull()
    // The no-matches state, NOT EmptyState's "No sessions yet" — which would
    // claim the user has never made a session. Matched exactly: nothing here is
    // a search, so copy that blames one is wrong even though the panel is right
    // (R9 successor to the loose /no sessions match/i this replaces, Task 10).
    expect(screen.getByText('No sessions match your search or filters.')).toBeTruthy()
    expect(screen.queryByText('No sessions yet')).toBeNull()
    expect(screen.getByLabelText('Status')).toHaveValue('error')

    await userEvent.click(screen.getByRole('button', { name: 'Clear filters' }))
    expect(screen.getByLabelText('Status')).toHaveValue('')
    expect(screen.getByRole('button', { name: 'Open session First' })).toBeTruthy()
  })

  it('the in-place reset clears the filter, not just the search', async () => {
    renderColumn({ sessions: [session({ id: 'sess-1', name: 'First', repo: 'o/a' })] })
    await userEvent.selectOptions(screen.getByLabelText('Status'), 'error')
    // Clearing only the search would leave this button inert and the list empty.
    // R9 successor to the `/clear search/i` name this replaces (Task 10): the
    // label now says it clears the filters too, matched exactly so a revert to
    // the search-only wording reddens (the old regex is a prefix of the new
    // label and would not have).
    await userEvent.click(screen.getByRole('button', { name: 'Clear search and filters' }))
    expect(screen.getByRole('button', { name: 'Open session First' })).toBeTruthy()
    expect(screen.getByLabelText('Status')).toHaveValue('')
  })
})

describe('SessionListColumn — accessibility', () => {
  it('has no axe violations with every section mounted', async () => {
    const { container } = renderColumn({
      sessions: [
        session({ id: 'sess-1', name: 'First' }),
        session({ id: 'sess-2', name: 'Waiting', status: 'queued', queuePosition: 1 }),
        session({ id: 'sess-3', name: 'Parked', status: 'pending_approval' }),
      ],
      selectedId: 'sess-1',
      adoptionEnabled: true,
    })
    expect(await axe(container)).toHaveNoViolations()
  })
})
