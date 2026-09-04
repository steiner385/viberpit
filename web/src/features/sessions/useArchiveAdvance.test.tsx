// `useArchiveAdvance` — the archive-advance behaviour ported out of `App.tsx`'s
// deleted `SessionRoute` (Task 17).
//
// ## Why this file exists now rather than in Task 18
//
// The assertions this covers live in `App.test.tsx` (mobile: archiving advances
// to the neighbour; advances even when the open session's group is collapsed;
// Back does NOT advance), and that whole file is red until Task 18 rebuilds it
// on the new shell. Porting a behaviour out of a file whose tests are red is
// exactly how a port becomes a silent no-op, so the ported hook is pinned here,
// against its own contract, in the same commit that moves it.
//
// ## Why the cache is seeded rather than the network faked
//
// The hooks (`useSessions`, `useArchivedSessions`) and the ordering
// (`useSessionOrdering`, `nextSessionAfterClose`) are all REAL — only the data
// they read is placed directly in the query cache. That makes "the session
// appeared in the archived list" a single deterministic write instead of a wait
// on a 30-second poll, and it keeps the failure modes honest: a hook that stops
// asking for the archived list, or an ordering that stops seeing the collapse
// state, still fails here.
import { describe, it, expect, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes, useLocation, useParams } from 'react-router-dom'

import { QK } from '@/api/hooks'
import type { ArchivedSession, Session } from '@/api/types'
import { archivedSession, session } from '@/test/factories'
import { useArchiveAdvance } from './useArchiveAdvance'

// ── harness ──────────────────────────────────────────────────────────────────

const seen: string[] = []

function LocationSpy() {
  seen.push(useLocation().pathname)
  return null
}

/** Stands in for the Sessions page: calls the hook and exposes its `onDone`. */
function SessionProbe() {
  const { id } = useParams()
  const onDone = useArchiveAdvance(id)
  return <button onClick={onDone}>archive</button>
}

interface Fixture {
  live?: Session[]
  archived?: ArchivedSession[]
}

function renderAt(path: string, { live = [], archived = [] }: Fixture = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  // Seeded before the first render, so the hook's pre-removal ordering snapshot
  // is taken from a populated list rather than from an empty loading frame.
  qc.setQueryData(QK.sessions, live)
  qc.setQueryData(QK.archived, archived)
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/sessions" element={<div>list</div>} />
          <Route path="/sessions/:id" element={<SessionProbe />} />
          <Route path="/archived/:id" element={<div>transcript</div>} />
        </Routes>
        <LocationSpy />
      </MemoryRouter>
    </QueryClientProvider>,
  )
  return qc
}

const TWO_LIVE = [session({ id: 'sess-1', name: 'First' }), session({ id: 'sess-2', name: 'Second' })]

beforeEach(() => {
  seen.length = 0
  localStorage.clear()
})

// ── tests ────────────────────────────────────────────────────────────────────

describe('useArchiveAdvance — onDone', () => {
  it('advances to the neighbour when the open session is archived', async () => {
    const user = userEvent.setup()
    renderAt('/sessions/sess-1', { live: TWO_LIVE })

    await user.click(await screen.findByRole('button', { name: 'archive' }))
    await waitFor(() => expect(seen).toContain('/sessions/sess-2'))
  })

  it("advances even when the open session's group is collapsed", async () => {
    // Both sessions are idle → the 'your-turn' group. Collapsed, the open
    // session is absent from the VISIBLE ordering, so the advance must fall
    // back to the full ordering rather than bail to /sessions.
    localStorage.setItem('ccswitch.sessionOrder', JSON.stringify({ groupBy: 'status', sortBy: 'status' }))
    localStorage.setItem('ccswitch.groupCollapse', JSON.stringify(['your-turn']))
    const user = userEvent.setup()
    renderAt('/sessions/sess-1', { live: TWO_LIVE })

    await user.click(await screen.findByRole('button', { name: 'archive' }))
    await waitFor(() => expect(seen).toContain('/sessions/sess-2'))
  })

  it('falls back to the list when the archived session was the only one', async () => {
    const user = userEvent.setup()
    renderAt('/sessions/sess-1', { live: [session({ id: 'sess-1' })] })

    await user.click(await screen.findByRole('button', { name: 'archive' }))
    await waitFor(() => expect(seen).toContain('/sessions'))
  })

  it('does not advance on its own — Back must still reach the list', async () => {
    renderAt('/sessions/sess-1', { live: TWO_LIVE })
    await screen.findByRole('button', { name: 'archive' })

    // Nothing archived, nothing clicked: the hook must be inert. (The Back
    // button's own navigation is the page's, not the hook's — this pins that
    // the hook does not hijack it.)
    await waitFor(() => expect(seen.length).toBeGreaterThan(0))
    expect(seen.every((p) => p === '/sessions/sess-1')).toBe(true)
  })
})

describe('useArchiveAdvance — the 503/409 fallback effect', () => {
  it('advances when a live session turns up in the archived list without onDone', async () => {
    const qc = renderAt('/sessions/sess-1', { live: TWO_LIVE })
    await screen.findByRole('button', { name: 'archive' })

    // The archive DID land, but the call errored — nobody called onDone.
    qc.setQueryData(QK.archived, [archivedSession({ id: 'sess-1' })])
    await waitFor(() => expect(seen).toContain('/sessions/sess-2'))
  })

  it('sends a session that was NEVER live to its read-only transcript', async () => {
    // A stale bookmark / automation-log link: /sessions/:id for a session that
    // is already archived. There is no neighbour to advance to.
    renderAt('/sessions/gone', {
      live: [session({ id: 'sess-2', name: 'Second' })],
      archived: [archivedSession({ id: 'gone' })],
    })

    await waitFor(() => expect(seen).toContain('/archived/gone'))
    // Never bounced through a live neighbour on the way.
    expect(seen).not.toContain('/sessions/sess-2')
  })
})
