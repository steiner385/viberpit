/**
 * Triage Inbox (design §8) — the pure keyboard reducer, then the component's
 * key handling and optimistic accept/reject/bulk paths.
 *
 * The keydown handler is bound to `window`, which is what makes the guard tests
 * below load-bearing rather than decorative: without them `a`/`r`/`x` fire while
 * the user types in any other input on the page, and `A` (i.e. Shift+a) turns a
 * capital letter in a search box into a bulk accept.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

import { inboxReducer, initialInboxState, isTypingTarget } from '../keyboardState'

// --- pure reducer (no DOM needed, but the file renders elsewhere) ---
describe('inboxReducer', () => {
  it('j/k move clamps to [0, count-1]', () => {
    let s = initialInboxState
    s = inboxReducer(s, { type: 'move', delta: 1, count: 3 }) // 0 -> 1
    s = inboxReducer(s, { type: 'move', delta: 1, count: 3 }) // 1 -> 2
    s = inboxReducer(s, { type: 'move', delta: 1, count: 3 }) // clamp at 2
    expect(s.cursor).toBe(2)
    s = inboxReducer(s, { type: 'move', delta: -5, count: 3 }) // clamp at 0
    expect(s.cursor).toBe(0)
  })

  it('move on an empty queue parks the cursor at 0', () => {
    const s = inboxReducer({ ...initialInboxState, cursor: 3 },
                           { type: 'move', delta: 1, count: 0 })
    expect(s.cursor).toBe(0)
  })

  it('x toggles selection', () => {
    let s = inboxReducer(initialInboxState, { type: 'toggleSelect', id: 'p1' })
    expect(s.selected.has('p1')).toBe(true)
    s = inboxReducer(s, { type: 'toggleSelect', id: 'p1' })
    expect(s.selected.has('p1')).toBe(false)
  })

  it('clearSelection empties the set (a bulk accept must not resend dead ids)', () => {
    let s = inboxReducer(initialInboxState, { type: 'toggleSelect', id: 'p1' })
    s = inboxReducer(s, { type: 'toggleSelect', id: 'p2' })
    expect(s.selected.size).toBe(2)
    s = inboxReducer(s, { type: 'clearSelection' })
    expect(s.selected.size).toBe(0)
  })

  it('rowsChanged clamps the cursor when the queue shrinks', () => {
    const s = inboxReducer({ ...initialInboxState, cursor: 4 },
                           { type: 'rowsChanged', count: 2 })
    expect(s.cursor).toBe(1)
  })

  it('rowsChanged to an empty queue parks the cursor at 0', () => {
    const s = inboxReducer({ ...initialInboxState, cursor: 4 },
                           { type: 'rowsChanged', count: 0 })
    expect(s.cursor).toBe(0)
  })

  it('e enters edit mode and cancel exits', () => {
    let s = inboxReducer(initialInboxState, { type: 'startEdit', id: 'p2' })
    expect(s.editing).toBe('p2')
    s = inboxReducer(s, { type: 'cancelEdit' })
    expect(s.editing).toBeNull()
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

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

async function renderInbox(rows: triage.TriageProposal[] = ROWS) {
  mocked.fetchTriage.mockResolvedValue(rows)
  const view = render(<TriageInbox />, { wrapper })
  if (rows.length) {
    await waitFor(() => expect(screen.getByText(rows[0].title!)).toBeInTheDocument())
  } else {
    await waitFor(() => expect(screen.getByText(/inbox zero/i)).toBeInTheDocument())
  }
  return view
}

beforeEach(() => {
  vi.clearAllMocks()
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

  it('`x` then `A` accepts the selected set in bulk', async () => {
    mocked.acceptBulk.mockResolvedValue({ accepted: 2, results: [] })
    await renderInbox()

    fireEvent.keyDown(window, { key: 'x' }) // select p1 (cursor 0)
    fireEvent.keyDown(window, { key: 'j' }) // cursor -> p2
    fireEvent.keyDown(window, { key: 'x' }) // select p2
    fireEvent.keyDown(window, { key: 'A' }) // accept selected

    await waitFor(() =>
      expect(mocked.acceptBulk).toHaveBeenCalledWith(expect.arrayContaining(['p1', 'p2'])))
    expect(mocked.acceptBulk.mock.calls[0][0]).toHaveLength(2)
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
})
