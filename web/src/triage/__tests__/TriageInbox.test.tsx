/**
 * Triage Inbox (design §8) — the pure keyboard reducer, then the component's
 * key handling and optimistic accept/reject/bulk paths.
 *
 * The keydown handler is bound to `window`, which is what makes the guard tests
 * below load-bearing rather than decorative: without them `a`/`r`/`x` fire while
 * the user types in any other input on the page, and `A` (i.e. Shift+a) turns a
 * capital letter in a search box into a bulk accept.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import type { ReactNode } from 'react'

import { pushOverlay, popOverlay, resetOverlayState } from '@/shell/useOverlayState'

import {
  inboxReducer, initialInboxState, isTypingTarget, resolveCursorIndex, scoreInputValue,
} from '../keyboardState'

const IDS = ['p1', 'p2', 'p3']

// --- pure reducer (no DOM needed, but the file renders elsewhere) ---
describe('inboxReducer', () => {
  it('j/k move clamps to [0, count-1] and anchors on the row it lands on', () => {
    let s = initialInboxState
    s = inboxReducer(s, { type: 'move', delta: 1, ids: IDS }) // 0 -> 1
    s = inboxReducer(s, { type: 'move', delta: 1, ids: IDS }) // 1 -> 2
    s = inboxReducer(s, { type: 'move', delta: 1, ids: IDS }) // clamp at 2
    expect(s.cursor).toBe(2)
    expect(s.cursorId).toBe('p3')
    s = inboxReducer(s, { type: 'move', delta: -5, ids: IDS }) // clamp at 0
    expect(s.cursor).toBe(0)
    expect(s.cursorId).toBe('p1')
  })

  it('move on an empty queue parks the cursor at 0', () => {
    const s = inboxReducer({ ...initialInboxState, cursor: 3, cursorId: 'p4' },
                           { type: 'move', delta: 1, ids: [] })
    expect(s.cursor).toBe(0)
    expect(s.cursorId).toBeNull()
  })

  it('x toggles selection', () => {
    let s = inboxReducer(initialInboxState, { type: 'toggleSelect', id: 'p1' })
    expect(s.selected.has('p1')).toBe(true)
    s = inboxReducer(s, { type: 'toggleSelect', id: 'p1' })
    expect(s.selected.has('p1')).toBe(false)
  })

  it('deselect drops one id (a single accept must not leave it for `A`)', () => {
    let s = inboxReducer(initialInboxState, { type: 'toggleSelect', id: 'p1' })
    s = inboxReducer(s, { type: 'toggleSelect', id: 'p2' })
    s = inboxReducer(s, { type: 'deselect', id: 'p1' })
    expect([...s.selected]).toEqual(['p2'])
    // A no-op deselect keeps the same object (no needless re-render).
    expect(inboxReducer(s, { type: 'deselect', id: 'p9' })).toBe(s)
  })

  it('clearSelection empties the set (a bulk accept must not resend dead ids)', () => {
    let s = inboxReducer(initialInboxState, { type: 'toggleSelect', id: 'p1' })
    s = inboxReducer(s, { type: 'toggleSelect', id: 'p2' })
    expect(s.selected.size).toBe(2)
    s = inboxReducer(s, { type: 'clearSelection' })
    expect(s.selected.size).toBe(0)
  })

  it('focusRow anchors the cursor where a pointer clicked', () => {
    const s = inboxReducer(initialInboxState, { type: 'focusRow', id: 'p3', index: 2 })
    expect(s.cursorId).toBe('p3')
    expect(s.cursor).toBe(2)
  })

  it('rowsChanged clamps the cursor when the queue shrinks', () => {
    const s = inboxReducer({ ...initialInboxState, cursor: 4 },
                           { type: 'rowsChanged', ids: ['p1', 'p2'] })
    expect(s.cursor).toBe(1)
    expect(s.cursorId).toBe('p2')
  })

  it('rowsChanged to an empty queue parks the cursor at 0', () => {
    const s = inboxReducer({ ...initialInboxState, cursor: 4, cursorId: 'p5' },
                           { type: 'rowsChanged', ids: [] })
    expect(s.cursor).toBe(0)
    expect(s.cursorId).toBeNull()
  })

  it('rowsChanged follows the anchored row when the queue is PREPENDED to', () => {
    const s = inboxReducer({ ...initialInboxState, cursor: 1, cursorId: 'p2' },
                           { type: 'rowsChanged', ids: ['p0', 'p1', 'p2', 'p3'] })
    expect(s.cursorId).toBe('p2')
    expect(s.cursor).toBe(2) // the index followed the row, not the other way round
  })

  it('e enters edit mode and cancel exits', () => {
    let s = inboxReducer(initialInboxState, { type: 'startEdit', id: 'p2' })
    expect(s.editing).toBe('p2')
    s = inboxReducer(s, { type: 'cancelEdit' })
    expect(s.editing).toBeNull()
  })
})

describe('resolveCursorIndex', () => {
  it('follows the anchored id wherever the row moved to', () => {
    const s = { ...initialInboxState, cursor: 0, cursorId: 'p3' }
    expect(resolveCursorIndex(IDS, s)).toBe(2)
  })

  it('falls back to the clamped index once the anchored row is gone', () => {
    const s = { ...initialInboxState, cursor: 1, cursorId: 'p2' }
    // p2 accepted away: the cursor lands on the row that took its place…
    expect(resolveCursorIndex(['p1', 'p3'], s)).toBe(1)
    // …clamped, when it was the last row.
    expect(resolveCursorIndex(['p1'], s)).toBe(0)
  })

  it('is -1 for an empty queue (indexing it yields undefined, which is handled)', () => {
    expect(resolveCursorIndex([], initialInboxState)).toBe(-1)
  })
})

describe('scoreInputValue', () => {
  it('maps a cleared field to undefined, NEVER 0', () => {
    // `Number('') === 0`: effort 0 is below RICE_MIN and 422s the PATCH (which
    // silently skips the chained accept); reach/impact/confidence 0 are in range
    // and persist as a real zero that collapses rice_score.
    expect(scoreInputValue('')).toBeUndefined()
    expect(scoreInputValue('  ')).toBeUndefined()
    expect(scoreInputValue('abc')).toBeUndefined()
    expect(scoreInputValue('0')).toBe(0) // an explicit 0 is still a value
    expect(scoreInputValue('4')).toBe(4)
  })
})

describe('isTypingTarget', () => {
  it('is true for the form controls a shortcut must never steal from', () => {
    for (const tag of ['input', 'textarea', 'select'] as const) {
      expect(isTypingTarget(document.createElement(tag)), tag).toBe(true)
    }
    const editable = document.createElement('div')
    editable.setAttribute('contenteditable', 'true')
    expect(isTypingTarget(editable)).toBe(true)
  })

  it('is false for ordinary elements, for window and for null', () => {
    expect(isTypingTarget(document.createElement('div'))).toBe(false)
    expect(isTypingTarget(window)).toBe(false)
    expect(isTypingTarget(null)).toBe(false)
  })
})

// --- component keyboard + optimistic accept ---
vi.mock('@/ws/client', () => ({ useSessionSocket: () => ({ on: vi.fn(), off: vi.fn() }) }))
vi.mock('@/api/triage', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/triage')>()
  return {
    ...actual,
    fetchTriage: vi.fn(),
    acceptProposal: vi.fn(),
    rejectProposal: vi.fn(),
    acceptBulk: vi.fn(),
    updateOverlay: vi.fn(),
  }
})

import { TriageInbox } from '../TriageInbox'
import { QK } from '@/api/hooks'
import * as triage from '@/api/triage'

const mocked = triage as unknown as {
  fetchTriage: ReturnType<typeof vi.fn>
  acceptProposal: ReturnType<typeof vi.fn>
  rejectProposal: ReturnType<typeof vi.fn>
  acceptBulk: ReturnType<typeof vi.fn>
  updateOverlay: ReturnType<typeof vi.fn>
}

function proposal(over: Partial<triage.TriageProposal>): triage.TriageProposal {
  return {
    id: 'p0', workItemId: 'w0', projectSlug: 'ccswitch', repo: 'o/r', number: 0,
    kind: 'issue', title: 'Untitled', url: null,
    proposedFeatureId: null, proposedFeatureSlug: null, noFitSuggestion: null,
    reach: null, impact: null, effort: null, confidence: null,
    rationale: null, confidenceLevel: 'low', status: 'pending', createdAt: null,
    ...over,
  }
}

const ROWS: triage.TriageProposal[] = [
  proposal({
    id: 'p1', workItemId: 'w1', number: 1, title: 'First',
    proposedFeatureId: 'f1', proposedFeatureSlug: 'sessions',
    reach: 3, impact: 2, effort: 1, confidence: 2, confidenceLevel: 'high',
  }),
  proposal({
    id: 'p2', workItemId: 'w2', number: 2, title: 'Second',
    noFitSuggestion: 'offline-mode',
    reach: 1, impact: 1, effort: 1, confidence: 1,
  }),
]

/** Three rows, so "the selected set" and "the whole queue" are distinguishable. */
const ROWS3: triage.TriageProposal[] = [
  ...ROWS,
  proposal({ id: 'p3', workItemId: 'w3', number: 3, title: 'Third' }),
]

// One client per test, reachable from the test body: the cursor-anchoring test
// has to mutate the cache *underneath* a rendered inbox, which is exactly the
// live condition (`useTriageLive` invalidates on every `/ws` status frame, and
// the server prepends newest-first) that a raw index cursor cannot survive.
let qc: QueryClient

function wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

/** The cache key `useTriage()` (no project scope) reads and writes. */
const TRIAGE_KEY = [...QK.triage, null]

/**
 * Let react-query reach its `mutationFn`, which it calls asynchronously.
 * Without this a `not.toHaveBeenCalled()` right after a `fireEvent` passes even
 * when the key DID fire — the call is still queued in a microtask.
 */
async function flush() {
  await act(async () => { await new Promise((r) => setTimeout(r, 0)) })
}

async function renderInbox(rows: triage.TriageProposal[] = ROWS, path = '/triage') {
  mocked.fetchTriage.mockResolvedValue(rows)
  const view = render(
    <MemoryRouter initialEntries={[path]}><TriageInbox /></MemoryRouter>,
    { wrapper },
  )
  if (rows.length) {
    await waitFor(() => expect(screen.getByText(rows[0].title!)).toBeInTheDocument())
  } else {
    await waitFor(() => expect(screen.getByText(/inbox zero/i)).toBeInTheDocument())
  }
  return view
}

beforeEach(() => {
  vi.clearAllMocks()
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  // Module state that outlives a test: a leaked push would silently disable
  // every inbox key in every test that follows.
  resetOverlayState()
})

afterEach(() => {
  resetOverlayState()
})

describe('TriageInbox', () => {
  it('renders the queue with a no_fit suggested name and per-project depth', async () => {
    await renderInbox([
      ...ROWS,
      proposal({ id: 'p3', workItemId: 'w3', number: 3, title: 'Third', projectSlug: 'kindash' }),
      proposal({ id: 'p4', workItemId: 'w4', number: 4, title: 'Fourth', projectSlug: null }),
    ])

    expect(screen.getByText('First')).toBeInTheDocument()
    // no_fit row surfaces the suggested new-feature name
    expect(screen.getByText(/offline-mode/)).toBeInTheDocument()
    // header depth comes from the server's projectSlug, NOT from the repo
    expect(screen.getByText('ccswitch: 2')).toBeInTheDocument()
    expect(screen.getByText('kindash: 1')).toBeInTheDocument()
    expect(screen.getByText('unknown: 1')).toBeInTheDocument()
  })

  it('scopes the queue to ?projectSlug= and shows a clearable scope chip', async () => {
    await renderInbox(ROWS, '/triage?projectSlug=example-product')
    expect(mocked.fetchTriage).toHaveBeenCalledWith('example-product')
    expect(screen.getByText(/scope: example-product/i)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /clear/i })).toHaveAttribute('href', '/triage')
  })

  it('names the scope in the empty state', async () => {
    await renderInbox([], '/triage?projectSlug=example-product')
    expect(screen.getByText(/inbox zero for example-product/i)).toBeInTheDocument()
  })

  it('`a` accepts the row under the cursor (optimistic, calls the API)', async () => {
    mocked.acceptProposal.mockResolvedValue({ id: 'p1', status: 'accepted' })
    await renderInbox()

    fireEvent.keyDown(window, { key: 'a' })

    await waitFor(() => expect(mocked.acceptProposal).toHaveBeenCalledWith('p1', undefined))
  })

  it('is optimistic — the row leaves the list before the server answers', async () => {
    // Never resolves, so `onSettled`'s refetch (which the static `fetchTriage`
    // mock would answer with the row still present) never runs and the only
    // thing that can remove the row is the optimistic `onMutate`.
    mocked.acceptProposal.mockReturnValue(new Promise(() => {}))
    await renderInbox()

    fireEvent.keyDown(window, { key: 'a' })

    await waitFor(() => expect(screen.queryByText('First')).not.toBeInTheDocument())
    expect(screen.getByText('Second')).toBeInTheDocument()
  })

  it('`j` moves the cursor, so `r` rejects the second row', async () => {
    mocked.rejectProposal.mockResolvedValue({ id: 'p2', status: 'rejected' })
    await renderInbox()

    fireEvent.keyDown(window, { key: 'j' })
    fireEvent.keyDown(window, { key: 'r' })

    await waitFor(() => expect(mocked.rejectProposal).toHaveBeenCalledWith('p2'))
  })

  it('`x` then `A` accepts EXACTLY the selected set in bulk', async () => {
    // Three rows, two selected: with a two-row fixture "selection works" and
    // "everything in the queue got sent" are the same assertion.
    mocked.acceptBulk.mockResolvedValue({ accepted: 2, results: [] })
    await renderInbox(ROWS3)

    fireEvent.keyDown(window, { key: 'x' }) // select p1 (cursor 0)
    fireEvent.keyDown(window, { key: 'j' }) // cursor -> p2
    fireEvent.keyDown(window, { key: 'j' }) // cursor -> p3
    fireEvent.keyDown(window, { key: 'x' }) // select p3, NOT p2
    fireEvent.keyDown(window, { key: 'A' }) // accept selected

    await waitFor(() =>
      expect(mocked.acceptBulk).toHaveBeenCalledWith(expect.arrayContaining(['p1', 'p3'])))
    expect(mocked.acceptBulk.mock.calls[0][0]).toEqual(expect.not.arrayContaining(['p2']))
    expect(mocked.acceptBulk.mock.calls[0][0]).toHaveLength(2)
  })

  it('a single `a` drops the row from the selection too', async () => {
    // Select p1 and p2, accept p1 with `a`, then `A`: resending p1 would come
    // back `not_pending` and the sweep's toast would say "Accepted 1".
    mocked.acceptProposal.mockResolvedValue({ id: 'p1', status: 'accepted' })
    mocked.acceptBulk.mockResolvedValue({ accepted: 1, results: [] })
    await renderInbox(ROWS3)

    fireEvent.keyDown(window, { key: 'x' }) // select p1
    fireEvent.keyDown(window, { key: 'j' })
    fireEvent.keyDown(window, { key: 'x' }) // select p2
    fireEvent.keyDown(window, { key: 'k' })
    fireEvent.keyDown(window, { key: 'a' }) // accept p1
    await waitFor(() => expect(mocked.acceptProposal).toHaveBeenCalledWith('p1', undefined))
    fireEvent.keyDown(window, { key: 'A' })

    await waitFor(() => expect(mocked.acceptBulk).toHaveBeenCalledWith(['p2']))
  })

  it('keeps the selection when a bulk accept FAILS and the rows come back', async () => {
    mocked.acceptBulk.mockRejectedValue(new Error('boom'))
    await renderInbox(ROWS3)

    fireEvent.keyDown(window, { key: 'x' })
    fireEvent.keyDown(window, { key: 'A' })
    await waitFor(() => expect(mocked.acceptBulk).toHaveBeenCalledTimes(1))

    // The rows rolled back, so the selection must have rolled back with them:
    // a retry has to resend the same set rather than silently sending nothing.
    fireEvent.keyDown(window, { key: 'A' })
    await waitFor(() => expect(mocked.acceptBulk).toHaveBeenCalledTimes(2))
    expect(mocked.acceptBulk.mock.calls[1][0]).toEqual(['p1'])
  })

  it('`e` opens the inline editor; submitting persists the overlay then accepts', async () => {
    mocked.updateOverlay.mockResolvedValue({ id: 'w1' })
    mocked.acceptProposal.mockResolvedValue({ id: 'p1', status: 'accepted' })
    await renderInbox()

    fireEvent.keyDown(window, { key: 'e' })
    const reach = await screen.findByLabelText('reach')
    fireEvent.change(reach, { target: { value: '5' } })
    fireEvent.click(screen.getByRole('button', { name: /save & accept/i }))

    await waitFor(() =>
      expect(mocked.updateOverlay).toHaveBeenCalledWith('w1', expect.objectContaining({ reach: 5 })))
    await waitFor(() =>
      expect(mocked.acceptProposal).toHaveBeenCalledWith('p1', expect.objectContaining({ reach: 5 })))
  })

  it('`e` focuses the first field, and Escape from inside it cancels', async () => {
    await renderInbox()

    fireEvent.keyDown(window, { key: 'e' })
    const reach = await screen.findByLabelText('reach')
    // Without autofocus a keyboard-only user has to Tab out of <body>, through
    // the whole nav rail, to reach the field the gesture just opened.
    expect(document.activeElement).toBe(reach)

    // Escape is the DOCUMENTED cancel, and the caret is in an input — a typing
    // target — so the guard must not eat it before the editor branch runs.
    fireEvent.keyDown(reach, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByLabelText('reach')).not.toBeInTheDocument())
    await flush()
    expect(mocked.updateOverlay).not.toHaveBeenCalled()
    expect(mocked.acceptProposal).not.toHaveBeenCalled()
  })

  it('CLEARING a score sends undefined, never 0', async () => {
    mocked.updateOverlay.mockResolvedValue({ id: 'w1' })
    mocked.acceptProposal.mockResolvedValue({ id: 'p1', status: 'accepted' })
    await renderInbox()

    fireEvent.keyDown(window, { key: 'e' })
    // p1 comes in with every score set; the user clears effort and reach.
    fireEvent.change(await screen.findByLabelText('effort'), { target: { value: '' } })
    fireEvent.change(screen.getByLabelText('reach'), { target: { value: '' } })
    fireEvent.click(screen.getByRole('button', { name: /save & accept/i }))

    await waitFor(() => expect(mocked.updateOverlay).toHaveBeenCalled())
    const edit = mocked.updateOverlay.mock.calls[0][1] as triage.OverlayEdits
    // `effort: 0` is below RICE_MIN and 422s the PATCH (killing the chained
    // accept); `reach: 0` is in range and persists as a real zero.
    expect(edit.effort).toBeUndefined()
    expect(edit.reach).toBeUndefined()
    expect(edit.impact).toBe(2)
  })

  it('does not leak an unhandled rejection when the overlay PATCH fails', async () => {
    const unhandled = vi.fn()
    window.addEventListener('unhandledrejection', unhandled)
    mocked.updateOverlay.mockRejectedValue(new Error('boom'))
    try {
      await renderInbox()
      fireEvent.keyDown(window, { key: 'e' })
      fireEvent.change(await screen.findByLabelText('reach'), { target: { value: '5' } })
      fireEvent.click(screen.getByRole('button', { name: /save & accept/i }))

      await waitFor(() => expect(mocked.updateOverlay).toHaveBeenCalled())
      await flush()
      // Behaviour is unchanged: no accept, and the editor stays open holding
      // the edit. What changes is that `obs/install.ts` no longer ships a
      // client-side `unhandledrejection` to the server log for it.
      expect(mocked.acceptProposal).not.toHaveBeenCalled()
      expect(screen.getByLabelText('reach')).toBeInTheDocument()
      expect(unhandled).not.toHaveBeenCalled()
    } finally {
      window.removeEventListener('unhandledrejection', unhandled)
    }
  })

  it('renders the classifier rationale (design §8)', async () => {
    await renderInbox([
      proposal({ id: 'p1', workItemId: 'w1', title: 'First',
                 rationale: 'Mentions the session list explicitly.' }),
    ])
    expect(screen.getByText('Mentions the session list explicitly.')).toBeInTheDocument()
  })

  // ── pointer parity: `/triage` is in the phone nav ──────────────────────────

  it('clicking a row moves the cursor, and its Accept button accepts THAT row', async () => {
    mocked.acceptProposal.mockResolvedValue({ id: 'p2', status: 'accepted' })
    await renderInbox()

    fireEvent.click(screen.getByText('Second'))
    fireEvent.click(screen.getByRole('button', { name: 'Accept Second' }))

    await waitFor(() => expect(mocked.acceptProposal).toHaveBeenCalledWith('p2', undefined))

    // The cursor moved with the click, so the keyboard picks up where the
    // pointer left off rather than back at row 1.
    expect(screen.getByText('First').closest('.triage-row'))
      .not.toHaveAttribute('aria-current')
  })

  it('has a per-row Reject and a select toggle for touch', async () => {
    mocked.rejectProposal.mockResolvedValue({ id: 'p1', status: 'rejected' })
    mocked.acceptBulk.mockResolvedValue({ accepted: 1, results: [] })
    await renderInbox(ROWS3)

    fireEvent.click(screen.getByRole('button', { name: 'Select Third' }))
    expect(screen.getByRole('button', { name: 'Select Third' }))
      .toHaveAttribute('aria-pressed', 'true')
    fireEvent.keyDown(window, { key: 'A' })
    await waitFor(() => expect(mocked.acceptBulk).toHaveBeenCalledWith(['p3']))

    fireEvent.click(screen.getByRole('button', { name: 'Reject First' }))
    await waitFor(() => expect(mocked.rejectProposal).toHaveBeenCalledWith('p1'))
  })

  // ── the window-listener guards ─────────────────────────────────────────────

  it('ignores shortcuts typed into an unrelated input, in and out of edit mode', async () => {
    await renderInbox()
    render(<input aria-label="search" />, { wrapper })
    const search = screen.getByLabelText('search')

    // `A` matters most: it is Shift+a, so any capital letter typed into a search
    // box would otherwise fire a bulk accept.
    for (const key of ['a', 'r', 'x', 'e', 'A', 'j', 'k']) {
      fireEvent.keyDown(search, { key })
    }

    expect(mocked.acceptProposal).not.toHaveBeenCalled()
    expect(mocked.rejectProposal).not.toHaveBeenCalled()
    expect(mocked.acceptBulk).not.toHaveBeenCalled()
    // The cursor never moved either: `a` from the window still hits the FIRST row.
    mocked.acceptProposal.mockResolvedValue({ id: 'p1', status: 'accepted' })
    fireEvent.keyDown(window, { key: 'a' })
    await waitFor(() => expect(mocked.acceptProposal).toHaveBeenCalledWith('p1', undefined))
  })

  it('ignores chords — Ctrl/Meta/Alt + a handled key is never the inbox shortcut', async () => {
    await renderInbox()

    fireEvent.keyDown(window, { key: 'a', ctrlKey: true })
    fireEvent.keyDown(window, { key: 'a', metaKey: true })
    fireEvent.keyDown(window, { key: 'r', altKey: true })

    expect(mocked.acceptProposal).not.toHaveBeenCalled()
    expect(mocked.rejectProposal).not.toHaveBeenCalled()
  })

  it('leaves an unhandled key alone (no preventDefault, no mutation)', async () => {
    await renderInbox()

    const handled = fireEvent.keyDown(window, { key: 'a' })
    const untouched = fireEvent.keyDown(window, { key: 'q' })

    // fireEvent returns false when a listener called preventDefault.
    expect(handled).toBe(false)
    expect(untouched).toBe(true)
  })

  it('`a` and `r` on an empty queue do nothing instead of throwing', async () => {
    await renderInbox([])

    fireEvent.keyDown(window, { key: 'j' })
    fireEvent.keyDown(window, { key: 'a' })
    fireEvent.keyDown(window, { key: 'r' })
    fireEvent.keyDown(window, { key: 'x' })
    fireEvent.keyDown(window, { key: 'e' })
    fireEvent.keyDown(window, { key: 'A' })

    expect(mocked.acceptProposal).not.toHaveBeenCalled()
    expect(mocked.rejectProposal).not.toHaveBeenCalled()
    expect(mocked.acceptBulk).not.toHaveBeenCalled()
    expect(screen.getByText(/inbox zero/i)).toBeInTheDocument()
  })

  /**
   * An accept is IRREVERSIBLE — `POST /pm/triage/{id}/accept` 409s anything that
   * is not `pending` and there is no un-accept route — so every guard below is
   * about a keystroke the user did not aim at the inbox.
   */
  it('fires nothing while an overlay is open (the route stays mounted under it)', async () => {
    await renderInbox()

    // `AppShell` renders `DrawerHost` BESIDE the route `Outlet`, so `/triage`
    // and its listener stay live under a Help/Assistant sheet. Below 1280 that
    // sheet is a modal focus trap whose first focused child is a <button> —
    // which `isTypingTarget` reports false for. Reading "a accept" in the help
    // sheet and pressing it must not accept the row underneath.
    act(() => pushOverlay())

    for (const key of ['a', 'r', 'x', 'e', 'A', 'j', 'k']) {
      fireEvent.keyDown(window, { key })
    }
    await flush()
    expect(mocked.acceptProposal).not.toHaveBeenCalled()
    expect(mocked.rejectProposal).not.toHaveBeenCalled()
    expect(mocked.acceptBulk).not.toHaveBeenCalled()
    expect(screen.queryByLabelText('reach')).not.toBeInTheDocument()

    // …and the cursor did not move under the overlay either: once the overlay
    // closes, `a` still hits the FIRST row.
    mocked.acceptProposal.mockResolvedValue({ id: 'p1', status: 'accepted' })
    act(() => popOverlay())
    fireEvent.keyDown(window, { key: 'a' })
    await waitFor(() => expect(mocked.acceptProposal).toHaveBeenCalledWith('p1', undefined))
  })

  it('leaves an event another handler already claimed alone (defaultPrevented)', async () => {
    await renderInbox()
    // Capture phase on `window` runs before the inbox's bubble-phase listener
    // for an event dispatched further down the tree — the same ordering that
    // makes a focused row's own `onKeyDown` win in the real app.
    const claim = (e: KeyboardEvent) => e.preventDefault()
    window.addEventListener('keydown', claim, true)
    try {
      fireEvent.keyDown(document.body, { key: 'a' })
      fireEvent.keyDown(document.body, { key: 'A' })
    } finally {
      window.removeEventListener('keydown', claim, true)
    }
    await flush()

    expect(mocked.acceptProposal).not.toHaveBeenCalled()
    expect(mocked.acceptBulk).not.toHaveBeenCalled()
  })

  it('accepts the row the cursor is ON when the queue reorders under it', async () => {
    mocked.acceptProposal.mockResolvedValue({ id: 'p2', status: 'accepted' })
    await renderInbox()

    fireEvent.keyDown(window, { key: 'j' }) // cursor -> p2 ("Second")

    // A new proposal lands between "the user reads the row" and "the user
    // presses `a`": the pm-triage loop produces these continuously and the
    // server returns them newest-first, so the row is PREPENDED and every index
    // below it shifts by one.
    act(() => {
      qc.setQueryData<triage.TriageProposal[]>(TRIAGE_KEY, [
        proposal({ id: 'p9', workItemId: 'w9', number: 9, title: 'Just arrived' }),
        ...ROWS,
      ])
    })
    await screen.findByText('Just arrived')

    fireEvent.keyDown(window, { key: 'a' })

    await waitFor(() => expect(mocked.acceptProposal).toHaveBeenCalledWith('p2', undefined))
  })

  it('scrolls the cursor row into view (the list is the scroll box)', async () => {
    const scrollIntoView = vi.fn()
    // jsdom implements no layout, so `scrollIntoView` is absent entirely.
    Object.defineProperty(Element.prototype, 'scrollIntoView', {
      value: scrollIntoView, configurable: true, writable: true,
    })
    try {
      await renderInbox()
      scrollIntoView.mockClear() // the mount-time anchor is not what is asserted
      fireEvent.keyDown(window, { key: 'j' })
      await waitFor(() => expect(scrollIntoView).toHaveBeenCalled())
    } finally {
      delete (Element.prototype as unknown as { scrollIntoView?: unknown }).scrollIntoView
    }
  })
})
