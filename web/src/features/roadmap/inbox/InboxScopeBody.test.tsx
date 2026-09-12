/**
 * The Inbox scope of the Roadmap list column (finish-line §4.3, roadmap spec
 * §12) — the rows, the grouping, the j/k/a/r/e/x/A key map re-homed from the
 * old Triage page, and the bulk bar.
 *
 * The keydown handler is bound to `window`, so the two guards below are
 * load-bearing rather than decorative: without them `a`/`r`/`x` fire while the
 * user types into the list's own search box, and `A` (Shift+a) turns a capital
 * letter into an irreversible bulk accept.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { cleanup, render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import type { TriageProposal } from '@/api/triage'
import { InboxScopeBody } from './InboxScopeBody'
import { clearInboxOrder, getInboxOrder } from './inboxOrderStore'
import { ViewportProvider } from '@/shell/useViewport'

const accept = vi.fn()
const reject = vi.fn()
const bulk = vi.fn()
const undo = vi.fn()
vi.mock('@/api/hooks', () => ({
  useAcceptProposal: () => ({ mutate: accept, isPending: false }),
  useRejectProposal: () => ({ mutate: reject, isPending: false }),
  useAcceptBulk: () => ({ mutate: bulk, isPending: false }),
  useTriageLive: () => undefined,
  // ProposalCard's own undo control (auto-accept chip) — mocked here too since
  // this file mounts the real ProposalCard rows.
  useUndoProposal: () => ({ mutate: undo, isPending: false }),
}))
const overlayOpen = vi.fn(() => false)
vi.mock('@/shell/useOverlayState', () => ({ overlayIsOpen: () => overlayOpen() }))

const p = (over: Partial<TriageProposal> & { id: string }): TriageProposal => ({
  workItemId: `w-${over.id}`, projectSlug: 'kindash', repo: 'cairnea/KinDash', number: 1, kind: 'issue',
  title: over.id, url: null, proposedFeatureId: null, proposedFeatureSlug: 'tasks', noFitSuggestion: null,
  reach: 3, impact: 3, effort: 2, confidence: 80, rationale: null, confidenceLevel: 'high', status: 'pending',
  createdAt: null, classifiedBy: null, resolvedAt: null, ...over,
})
/** Ordered (group by product, then id): First, Third | Second. Three rows so
 *  "the selected set" and "the whole queue" are different assertions. */
const ROWS = [
  p({ id: 'First' }),
  p({ id: 'Second', projectSlug: 'viberpit', repo: 'steiner385/viberpit' }),
  p({ id: 'Third' }),
]

function mount(over: Partial<React.ComponentProps<typeof InboxScopeBody>> = {}) {
  const onOpen = vi.fn()
  const onClearProduct = vi.fn()
  const tree = (props: Partial<React.ComponentProps<typeof InboxScopeBody>>) => (
    <ViewportProvider kiosk={null}>
      <MemoryRouter>
        <InboxScopeBody proposals={ROWS} isLoading={false} isError={false} product={null}
          selectedKey={null} onOpen={onOpen} onClearProduct={onClearProduct}
          {...over} {...props} />
      </MemoryRouter>
    </ViewportProvider>
  )
  const { unmount, rerender } = render(tree({}))
  // Re-render with the same component instance (state preserved) — how a poll
  // landing new server rows under a live selection is reproduced.
  return {
    onOpen, onClearProduct, unmount,
    rerender: (next: Partial<React.ComponentProps<typeof InboxScopeBody>>) =>
      rerender(tree(next)),
  }
}

// jsdom implements no scrolling, so the row's scroll-into-view is only
// observable as a stub. It is also the only way to assert the guard that makes
// `a` safe (see the cursor test below).
const scrollIntoView = vi.fn()

describe('InboxScopeBody', () => {
  beforeEach(() => {
    accept.mockReset(); reject.mockReset(); bulk.mockReset(); undo.mockReset()
    overlayOpen.mockReturnValue(false)
    localStorage.clear()
    clearInboxOrder()
    scrollIntoView.mockReset()
    Element.prototype.scrollIntoView = scrollIntoView
  })

  afterEach(() => {
    Reflect.deleteProperty(Element.prototype, 'scrollIntoView')
  })

  it('groups by product by default and opens a row', () => {
    const { onOpen } = mount()
    expect(screen.getByText('kindash')).toBeInTheDocument()
    expect(screen.getByText('viberpit')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Open proposal Second' }))
    expect(onOpen).toHaveBeenCalledWith(expect.objectContaining({ id: 'Second' }))
  })

  it('states the repo, the number, the scores and the proposed capability', () => {
    mount()
    expect(screen.getAllByText(/cairnea\/KinDash#1 · R3 I3 E2 C80/)).not.toHaveLength(0)
    expect(screen.getAllByText('tasks')).not.toHaveLength(0)
  })

  it('says the confidence in words, not only in the rail colour', () => {
    // The rail, the tier dot and the badge tone all encode confidence as colour
    // alone; a screen reader (and anyone who cannot tell the tiers apart) needs
    // the same fact in text.
    mount({ proposals: [p({ id: 'Sure' }), p({ id: 'Unsure', confidenceLevel: 'low' })] })
    expect(screen.getByText('high confidence')).toBeInTheDocument()
    expect(screen.getByText('low confidence')).toBeInTheDocument()
    // A row the classifier gave no level reads as the safest thing it could be.
    cleanup()
    mount({ proposals: [p({ id: 'Blank', confidenceLevel: null })] })
    expect(screen.getByText('low confidence')).toBeInTheDocument()
  })

  it('names the suggested new capability when the proposal fits nowhere', () => {
    mount({ proposals: [p({ id: 'Homeless', proposedFeatureSlug: null, noFitSuggestion: 'offline-mode' })] })
    expect(screen.getByText(/no fit → offline-mode/)).toBeInTheDocument()
  })

  it('j/k move the cursor, a accepts the cursor row, r rejects', () => {
    mount()
    // Cursor starts on the first ordered row (First); `j` lands it on Third.
    fireEvent.keyDown(window, { key: 'j' })
    fireEvent.keyDown(window, { key: 'a' })
    expect(accept).toHaveBeenCalledWith({ id: 'Third' })
    fireEvent.keyDown(window, { key: 'k' })
    fireEvent.keyDown(window, { key: 'r' })
    expect(reject).toHaveBeenCalledWith({ id: 'First' })
  })

  it('a/r/e/x do nothing when the cursor sits on a non-pending row', () => {
    // A fresh auto-accept rides along in the list for the undo window — it has
    // already had its one decision, so the keys that make a decision or select
    // for a bulk one must not treat the cursor landing there as a request.
    mount({ proposals: [p({ id: 'First', status: 'accepted', classifiedBy: 'auto', resolvedAt: '2026-09-12T10:00:00Z' }), p({ id: 'Second' })] })
    fireEvent.keyDown(window, { key: 'a' })
    fireEvent.keyDown(window, { key: 'r' })
    fireEvent.keyDown(window, { key: 'x' })
    fireEvent.keyDown(window, { key: 'e' })
    expect(accept).not.toHaveBeenCalled()
    expect(reject).not.toHaveBeenCalled()
    expect(screen.queryByRole('region', { name: 'Bulk actions' })).toBeNull()
  })

  it('scrolls the cursor row into view — `a` must never accept a row off-screen', () => {
    mount()
    scrollIntoView.mockReset()
    fireEvent.keyDown(window, { key: 'j' })
    // The row `j` landed on is the one that scrolled, and it is the one `a`
    // would accept.
    const row = screen.getByTestId('row-proposal:Third')
    expect(row).toHaveAttribute('aria-current', 'true')
    expect(scrollIntoView).toHaveBeenCalled()
    expect(scrollIntoView.mock.instances).toContain(row)
  })

  it('x selects and A accepts exactly the selection through the bulk bar', () => {
    mount()
    fireEvent.keyDown(window, { key: 'x' })
    fireEvent.keyDown(window, { key: 'j' })
    fireEvent.keyDown(window, { key: 'x' })
    expect(screen.getByText('2 selected')).toBeInTheDocument()
    // In the pinned controls strip, not inside the scroll band — a bulk bar
    // below the rows scrolls away from the selection that summoned it.
    expect(screen.getByTestId('session-list-controls'))
      .toContainElement(screen.getByRole('region', { name: 'Bulk actions' }))
    fireEvent.click(screen.getByRole('button', { name: /Accept selected/ }))
    expect(bulk).toHaveBeenCalledWith(expect.arrayContaining([expect.any(String)]), expect.anything())
    expect(bulk.mock.calls[0][0]).toEqual(['First', 'Third'])
  })

  it('drops a selected row from the selection once it stops being pending', () => {
    // A fresh auto-accept STAYS in the list for its undo window, so a row the
    // user ticked while pending can go `accepted` in place under their hands
    // (the PM Triage loop landing between polls). Its tick then renders both
    // checked and disabled, and the bulk bar counts a row nothing can act on.
    const { rerender } = mount()
    fireEvent.keyDown(window, { key: 'x' })
    fireEvent.keyDown(window, { key: 'j' })
    fireEvent.keyDown(window, { key: 'x' })
    expect(screen.getByText('2 selected')).toBeInTheDocument()

    const autoAccepted = (id: string) => ROWS.map((r) => (
      r.id === id ? { ...r, status: 'accepted' as const, classifiedBy: 'auto' } : r))
    rerender({ proposals: autoAccepted('First') })
    expect(screen.getByText('1 selected')).toBeInTheDocument()

    // …and when the last selected row goes too, the bulk bar leaves entirely.
    rerender({
      proposals: ROWS.map((r) => (
        r.id === 'First' || r.id === 'Third'
          ? { ...r, status: 'accepted' as const, classifiedBy: 'auto' } : r)),
    })
    expect(screen.queryByRole('region', { name: 'Bulk actions' })).toBeNull()
  })

  it('A with nothing selected accepts nothing', () => {
    mount()
    fireEvent.keyDown(window, { key: 'A' })
    expect(bulk).not.toHaveBeenCalled()
  })

  it('the per-row checkbox is the pointer parity for x', () => {
    mount()
    const box = screen.getByRole('button', { name: 'Select Second' })
    expect(box).toHaveAttribute('aria-pressed', 'false')
    fireEvent.click(box)
    expect(screen.getByRole('button', { name: 'Select Second' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByText('1 selected')).toBeInTheDocument()
  })

  it('e opens the cursor row for editing', () => {
    const { onOpen } = mount()
    fireEvent.keyDown(window, { key: 'e' })
    expect(onOpen).toHaveBeenCalledWith(expect.objectContaining({ id: 'First' }), { edit: true })
  })

  it('ignores keys typed into inputs and while an overlay is open', () => {
    mount()
    fireEvent.keyDown(screen.getByRole('searchbox', { name: 'Search inbox' }), { key: 'a' })
    expect(accept).not.toHaveBeenCalled()

    overlayOpen.mockReturnValue(true)
    fireEvent.keyDown(window, { key: 'a' })
    expect(accept).not.toHaveBeenCalled()
  })

  it('shows the product chip and clears it', () => {
    const { onClearProduct } = mount({ product: 'kindash' })
    expect(screen.getByText(/Scope: kindash/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Show every product' }))
    expect(onClearProduct).toHaveBeenCalled()
  })

  it('search narrows the queue', () => {
    mount()
    fireEvent.change(screen.getByRole('searchbox', { name: 'Search inbox' }),
                     { target: { value: 'steiner385' } })
    expect(screen.getByRole('button', { name: 'Open proposal Second' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Open proposal First' })).toBeNull()
  })

  it('publishes the RENDERED order, and republishes it when the sort control changes', () => {
    // The detail pane advances through this list (`inboxOrderStore`), so a
    // group/sort/search change made here has to reach it — the whole point of
    // the store is that the pane cannot compute the order for itself.
    const rows = [
      p({ id: 'zeta', title: 'Zeta', createdAt: '2026-09-08T10:00:00Z' }),
      p({ id: 'alpha', title: 'Alpha', createdAt: '2026-09-07T10:00:00Z' }),
    ]
    const { unmount } = mount({ proposals: rows })
    expect([...getInboxOrder()]).toEqual(['zeta', 'alpha'])

    fireEvent.change(screen.getByRole('combobox', { name: 'Sort by' }), { target: { value: 'title' } })
    expect([...getInboxOrder()]).toEqual(['alpha', 'zeta'])

    // Search is part of "rendered": accepting the open row should advance to
    // one the user can actually see.
    fireEvent.change(screen.getByRole('searchbox', { name: 'Search inbox' }), { target: { value: 'zeta' } })
    expect([...getInboxOrder()]).toEqual(['zeta'])

    // Gone from the screen (the phone layout unmounts the column): the pane
    // must fall back rather than follow a frozen order.
    unmount()
    expect([...getInboxOrder()]).toEqual([])
  })

  it('marks the open proposal selected — the URL IS the selection', () => {
    mount({ selectedKey: 'proposal:Second' })
    expect(screen.getByRole('button', { name: 'Open proposal Second' }))
      .toHaveAttribute('data-selected', 'true')
    expect(screen.getByRole('button', { name: 'Open proposal First' }))
      .toHaveAttribute('data-selected', 'false')
  })

  it('empty, loading and error states', () => {
    const { onClearProduct } = mount({ proposals: [], isLoading: false })
    expect(screen.getByText(/Inbox zero/)).toBeInTheDocument()
    expect(onClearProduct).not.toHaveBeenCalled()
  })

  it('names the product in the empty state, and the way out is still there', () => {
    const { onClearProduct } = mount({ proposals: [], isLoading: false, product: 'kindash' })
    expect(screen.getByText(/Inbox zero for kindash/)).toBeInTheDocument()
    // An empty SCOPED inbox is exactly when the widening control matters most:
    // without it "Inbox zero for kindash" is a dead end.
    fireEvent.click(screen.getByRole('button', { name: 'Show every product' }))
    expect(onClearProduct).toHaveBeenCalled()
  })

  it('shows a skeleton while loading', () => {
    mount({ proposals: [], isLoading: true })
    expect(screen.getByRole('status', { name: 'Loading inbox' })).toBeInTheDocument()
  })

  it('says so when the inbox could not be loaded', () => {
    mount({ proposals: [], isError: true })
    expect(screen.getByText('Could not load the inbox.')).toBeInTheDocument()
  })
})
