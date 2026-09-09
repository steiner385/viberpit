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
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import type { TriageProposal } from '@/api/triage'
import { InboxScopeBody } from './InboxScopeBody'

const accept = vi.fn()
const reject = vi.fn()
const bulk = vi.fn()
vi.mock('@/api/hooks', () => ({
  useAcceptProposal: () => ({ mutate: accept, isPending: false }),
  useRejectProposal: () => ({ mutate: reject, isPending: false }),
  useAcceptBulk: () => ({ mutate: bulk, isPending: false }),
  useTriageLive: () => undefined,
}))
const overlayOpen = vi.fn(() => false)
vi.mock('@/shell/useOverlayState', () => ({ overlayIsOpen: () => overlayOpen() }))

const p = (over: Partial<TriageProposal> & { id: string }): TriageProposal => ({
  workItemId: `w-${over.id}`, projectSlug: 'kindash', repo: 'cairnea/KinDash', number: 1, kind: 'issue',
  title: over.id, url: null, proposedFeatureId: null, proposedFeatureSlug: 'tasks', noFitSuggestion: null,
  reach: 3, impact: 3, effort: 2, confidence: 80, rationale: null, confidenceLevel: 'high', status: 'pending',
  createdAt: null, ...over,
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
  render(
    <MemoryRouter>
      <InboxScopeBody proposals={ROWS} isLoading={false} isError={false} product={null}
        selectedKey={null} onOpen={onOpen} onClearProduct={onClearProduct} {...over} />
    </MemoryRouter>,
  )
  return { onOpen, onClearProduct }
}

describe('InboxScopeBody', () => {
  beforeEach(() => {
    accept.mockReset(); reject.mockReset(); bulk.mockReset()
    overlayOpen.mockReturnValue(false)
    localStorage.clear()
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

  it('x selects and A accepts exactly the selection through the bulk bar', () => {
    mount()
    fireEvent.keyDown(window, { key: 'x' })
    fireEvent.keyDown(window, { key: 'j' })
    fireEvent.keyDown(window, { key: 'x' })
    expect(screen.getByText('2 selected')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Accept selected/ }))
    expect(bulk).toHaveBeenCalledWith(expect.arrayContaining([expect.any(String)]), expect.anything())
    expect(bulk.mock.calls[0][0]).toEqual(['First', 'Third'])
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

  it('names the product in the empty state when the inbox is scoped', () => {
    mount({ proposals: [], isLoading: false, product: 'kindash' })
    expect(screen.getByText(/Inbox zero for kindash/)).toBeInTheDocument()
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
