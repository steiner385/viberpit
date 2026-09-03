// ContextList — the `app.list` region.
//
// Two properties carry this file:
//
//   1. The region has an ACCESSIBLE NAME. Task 9's column deliberately dropped
//      phone's <h2>Sessions</h2>, and the `aria-label="Sessions"` left on the
//      role-less #session-listbox div is inert (assistive tech ignores
//      aria-label on a generic role), so the surface had no name at all. The
//      name is asserted here, where the landmark lives.
//
//   2. The list does NOT REMOUNT when `:id` changes. That is the whole reason
//      `app.list` is a region rather than part of the page: DesktopShell wrapped
//      the entire shell in a SessionRuntimeProvider keyed on the focused session
//      id, so every click remounted the column and reset its scrollTop, which it
//      papered over with a `listScrollTop` + `restoreListScroll` callback-ref
//      pair. Task 9 dropped that workaround; the identity assertion below
//      (`expect(before).toBe(after)` on the scroll NODE, not its presence) is
//      what makes dropping it safe. `toBeInTheDocument()` would pass across a
//      remount and prove nothing.
//
// Scope note on axe: jsdom applies no stylesheet, so axe here covers roles,
// names and ARIA wiring ONLY. Its `color-contrast` rule always returns
// `incomplete` and `toHaveNoViolations()` inspects `violations` alone — a green
// assertion below is NOT evidence of adequate contrast. Contrast is checked
// separately by src/__tests__/contrast.test.ts, which does the WCAG maths on the
// tokens in index.css.
import * as React from 'react'
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { axe, toHaveNoViolations } from 'jest-axe'
import { MemoryRouter, matchPath, useLocation, useNavigate } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

expect.extend(toHaveNoViolations)

// Partial mock: the daemon-health query needs the API layer and this region does
// not care what it says. Every other hook the column uses stays real.
vi.mock('@/api/hooks', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/hooks')>()
  return { ...actual, useDaemonHealth: () => ({ data: undefined }) }
})

import type { Account, Session } from '@/api/types'
import { NAV_DEFS } from '@/shell/navDefs'
import { stubMatchMedia, WIDTH_TIER_QUERIES } from '@/test/matchMedia'
import { ViewportProvider } from '@/shell/useViewport'
import { SIDEBAR_MAX, SIDEBAR_STORAGE_KEY } from '@/lib/sidebarWidth'
import { useSessionListState } from '@/features/sessions/useSessionListState'
import { ContextList } from './ContextList'

// ── typed factories (R10: real values, never `as unknown as T`) ───────────────

function session(over: Partial<Session> = {}): Session {
  return {
    id: 'a',
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

const SESSIONS = [session({ id: 'a', name: 'First' }), session({ id: 'b', name: 'Second' })]
const ACCOUNTS = [account()]
const noop = () => {}

// ── viewport harness ─────────────────────────────────────────────────────────

const TIERS = WIDTH_TIER_QUERIES

/** Navigates on click — a real route change, not a remount of the tree. */
function GoTo({ to }: { to: string }) {
  const navigate = useNavigate()
  return (
    <button type="button" onClick={() => navigate(to)}>
      go {to}
    </button>
  )
}

/**
 * Mounts `ContextList` the way `AppShell` will: the list state lives ABOVE the
 * region (the prompt bar reads the same handle), and `selectedId` is derived
 * from the route, so a `:id` change churns the region's props on every
 * navigation. That prop churn is precisely what the identity test must survive.
 */
function Harness({ sessions = SESSIONS }: { sessions?: Session[] }) {
  const state = useSessionListState(sessions)
  const { pathname } = useLocation()
  // The region sits OUTSIDE the route tree, so the open session is read from the
  // path rather than from `useParams()` — the same way AppShell will.
  const id = matchPath('/sessions/:id', pathname)?.params.id ?? null
  return (
    <ContextList
      sessions={{
        state,
        accounts: ACCOUNTS,
        selectedId: id,
        newSelected: id === null,
        onNew: noop,
        onOpen: noop,
        onRename: noop,
        onClose: noop,
        onDelete: noop,
        onQuickResume: noop,
      }}
    />
  )
}

function renderList({
  path = '/sessions',
  tier = 'desktop' as keyof typeof TIERS,
  sessions,
}: { path?: string; tier?: keyof typeof TIERS; sessions?: Session[] } = {}) {
  stubMatchMedia(TIERS[tier])
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <ViewportProvider kiosk={null}>
          <GoTo to="/sessions/b" />
          <Harness sessions={sessions} />
        </ViewportProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

const region = (container: HTMLElement) => container.querySelector('[data-region="app.list"]')

beforeEach(() => {
  localStorage.clear()
  vi.unstubAllGlobals()
})

// ── tests ────────────────────────────────────────────────────────────────────

describe('ContextList — which sections get a list', () => {
  it('renders the session list on /sessions, in a region marked app.list', () => {
    const { container } = renderList({ path: '/sessions' })
    expect(region(container)).toBeTruthy()
    expect(screen.getByTestId('session-list-scroll')).toBeTruthy()
    expect(screen.getByRole('button', { name: /new session/i })).toBeTruthy()
  })

  it('renders the session list on /sessions/:id too', () => {
    const { container } = renderList({ path: '/sessions/a' })
    expect(region(container)).toBeTruthy()
    expect(screen.getByTestId('session-list-scroll')).toBeTruthy()
  })

  it('renders NOTHING on /pipeline — the region node is absent, not empty', () => {
    const { container } = renderList({ path: '/pipeline' })
    expect(region(container)).toBeNull()
    expect(screen.queryByTestId('session-list-scroll')).toBeNull()
    // and no orphaned landmark or resizer left behind
    expect(screen.queryByRole('complementary')).toBeNull()
    expect(screen.queryByRole('separator')).toBeNull()
  })

  it('renders nothing for every section whose navDef says hasList: false', () => {
    // Derived from NAV_DEFS, never a hardcoded id list — a new section that
    // forgets `hasList` is caught here rather than shipping a stray column.
    for (const def of NAV_DEFS.filter((d) => !d.hasList)) {
      const { container, unmount } = renderList({ path: def.path })
      expect(region(container), `${def.path} should have no list column`).toBeNull()
      unmount()
    }
  })
})

describe('ContextList — accessible name', () => {
  it('is a landmark named for its section', () => {
    const { container } = renderList({ path: '/sessions' })
    const named = screen.getByRole('complementary', { name: 'Sessions' })
    expect(named).toBe(region(container))
  })

  it('names the region from the navDef label, not a second copy of the string', () => {
    // The visible/hidden heading and the nav item read the same NAV_DEFS row, so
    // renaming a section cannot leave the landmark announcing the old name.
    const sessionsDef = NAV_DEFS.find((d) => d.id === 'sessions')
    expect(sessionsDef?.hasList).toBe(true)
    renderList({ path: '/sessions' })
    expect(screen.getByRole('heading', { name: sessionsDef!.label })).toBeTruthy()
  })

  it('has no axe violations', async () => {
    const { container } = renderList({ path: '/sessions/a' })
    expect(await axe(container)).toHaveNoViolations()
  })
})

describe('ContextList — the resizer', () => {
  it.each(['desktop', 'wide'] as const)('renders SidebarResizer at %s', (tier) => {
    renderList({ tier })
    expect(screen.getByRole('separator', { name: /resize/i })).toBeTruthy()
  })

  it.each(['phone', 'tablet'] as const)('renders no SidebarResizer at %s', (tier) => {
    renderList({ tier })
    expect(screen.queryByRole('separator')).toBeNull()
    // the list itself is still there — only the drag handle is tier-gated
    expect(screen.getByTestId('session-list-scroll')).toBeTruthy()
  })

  it('drives the column width and persists it under the migrated key', async () => {
    const user = userEvent.setup()
    const { container } = renderList({ tier: 'desktop' })
    const handle = screen.getByRole('separator', { name: /resize/i })
    handle.focus()
    await user.keyboard('{End}')
    expect(region(container)).toHaveStyle({ width: `${SIDEBAR_MAX}px` })
    expect(localStorage.getItem(SIDEBAR_STORAGE_KEY)).toBe(String(SIDEBAR_MAX))
  })
})

describe('ContextList — the list survives a :id change', () => {
  it('keeps the SAME scroll node when the route moves from /sessions/a to /sessions/b', async () => {
    const user = userEvent.setup()
    renderList({ path: '/sessions/a' })

    const before = screen.getByTestId('session-list-scroll')
    before.scrollTop = 240

    await user.click(screen.getByRole('button', { name: 'go /sessions/b' }))

    const after = screen.getByTestId('session-list-scroll')
    // Identity, not presence: a remount would hand back a fresh node that is
    // equally "in the document" and equally non-null.
    expect(after).toBe(before)
    // …and therefore the scroll position needs no restoring workaround.
    expect(after.scrollTop).toBe(240)
  })

  it('is not frozen: props still flow through on the same DOM nodes', async () => {
    const user = userEvent.setup()
    renderList({ path: '/sessions' })
    const newButton = screen.getByRole('button', { name: /new session/i })
    const before = screen.getByTestId('session-list-scroll')
    expect(newButton).toHaveAttribute('data-selected', 'true')

    await user.click(screen.getByRole('button', { name: 'go /sessions/b' }))

    // Same nodes, updated props — a re-render, which is the point, rather than a
    // remount (which would also flip the attribute and prove nothing).
    expect(screen.getByTestId('session-list-scroll')).toBe(before)
    expect(screen.getByRole('button', { name: /new session/i })).toBe(newButton)
    expect(newButton).toHaveAttribute('data-selected', 'false')
  })
})
