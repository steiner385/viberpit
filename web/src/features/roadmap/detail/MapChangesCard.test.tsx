// The product page's map-changes PR card (spec §9, R8c-3): nothing while there
// is no open pointer, otherwise a Banner keyed off `behind`/`conflicting`, with
// Abandon (typed-slug confirm) on every variant.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, within } from '@testing-library/react'
import type { ProductNode, MapChangesPointer } from '@/api/roadmap'
import { MapChangesCard } from './MapChangesCard'

const h = vi.hoisted(() => ({
  abandon: vi.fn(),
  rebase: vi.fn(),
  abandonState: { isPending: false },
  rebaseState: { isPending: false },
}))
vi.mock('@/api/hooks', () => ({
  useAbandonMapChanges: () => ({ mutate: h.abandon, ...h.abandonState }),
  useRebaseMapChanges: () => ({ mutate: h.rebase, ...h.rebaseState }),
}))

function pointer(over: Partial<MapChangesPointer> = {}): MapChangesPointer {
  return {
    branch: 'pm/map-changes-kindash',
    prNumber: 12,
    prUrl: 'https://github.com/steiner385/viberpit/pull/12',
    status: 'open',
    openedAt: '2026-09-11T00:00:00Z',
    headSha: 'abc123',
    behind: false,
    conflicting: false,
    ...over,
  }
}

function product(mapChanges: MapChangesPointer | null): ProductNode {
  return {
    slug: 'kindash', name: 'KinDash', tier: 'idle',
    counts: { inFlight: 0, blocked: 0, needsYou: 0, capabilities: 0, awaitingTriage: 0, needsHome: 0 },
    capabilities: [], initiatives: [], mapChanges,
  }
}

const NOW = new Date('2026-09-12T00:00:00Z')

beforeEach(() => {
  h.abandon.mockReset(); h.rebase.mockReset()
  Object.assign(h.abandonState, { isPending: false })
  Object.assign(h.rebaseState, { isPending: false })
  vi.useFakeTimers().setSystemTime(NOW)
})

afterEach(() => {
  vi.useRealTimers()
})

describe('MapChangesCard', () => {
  it('renders nothing when the product has no map-changes pointer', () => {
    const { container } = render(<MapChangesCard product={product(null)} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('renders nothing once the pointer has merged', () => {
    const { container } = render(<MapChangesCard product={product(pointer({ status: 'merged' }))} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('info variant: links the PR and shows how long ago it opened', () => {
    render(<MapChangesCard product={product(pointer())} />)
    const link = screen.getByRole('link', { name: 'PR #12' })
    expect(link).toHaveAttribute('href', 'https://github.com/steiner385/viberpit/pull/12')
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', 'noreferrer')
    expect(screen.getByRole('status')).toHaveTextContent('Capability map changes are open')
    expect(screen.getByRole('status')).toHaveTextContent('opened')
    expect(screen.getByRole('status')).toHaveTextContent('ago')
    expect(screen.queryByRole('button', { name: 'Rebase' })).toBeNull()
  })

  it('behind: warning banner offers Rebase, which mutates', () => {
    render(<MapChangesCard product={product(pointer({ behind: true }))} />)
    expect(screen.getByRole('status')).toHaveTextContent('Capability map changes are behind')
    fireEvent.click(screen.getByRole('button', { name: 'Rebase' }))
    expect(h.rebase).toHaveBeenCalled()
  })

  it('conflicting: danger copy naming GitHub, no Rebase button', () => {
    render(<MapChangesCard product={product(pointer({ conflicting: true }))} />)
    const status = screen.getByRole('status')
    expect(status).toHaveTextContent('Capability map changes conflict with the default branch')
    expect(status).toHaveTextContent('Resolve the conflict on GitHub; the roadmap will not force-push.')
    expect(screen.queryByRole('button', { name: 'Rebase' })).toBeNull()
  })

  // Spec §9 asks ONE card for "the open PR, its age, and Rebase when behind" —
  // so the PR line is the same on every variant. Before this, the two variants a
  // user most needs the PR from (behind, conflicting) were the two that hid it.
  it.each([
    ['behind', { behind: true }],
    ['conflicting', { conflicting: true }],
  ] as const)('%s still carries the PR link and its age', (_name, over) => {
    render(<MapChangesCard product={product(pointer(over))} />)
    expect(screen.getByRole('link', { name: 'PR #12' }))
      .toHaveAttribute('href', 'https://github.com/steiner385/viberpit/pull/12')
    expect(screen.getByRole('status')).toHaveTextContent('opened')
    expect(screen.getByRole('status')).toHaveTextContent('ago')
  })

  // A pointer is written when the branch is created; the number arrives with the
  // PR. "PR #null" is the one thing the card must never say.
  it('says it without a number rather than "PR #null", and offers no link', () => {
    render(<MapChangesCard product={product(pointer({ prNumber: null }))} />)
    const status = screen.getByRole('status')
    expect(status).toHaveTextContent('the map changes PR')
    expect(status).not.toHaveTextContent('PR #null')
    // A URL without a number is not a link worth offering.
    expect(screen.queryByRole('link')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Abandon' }))
    const dialog = within(screen.getByRole('dialog'))
    expect(dialog.getByText(/Closes the map changes PR/)).toBeInTheDocument()
    expect(screen.queryByText(/PR #null/)).toBeNull()
  })

  it('every variant carries an Abandon action', () => {
    render(<MapChangesCard product={product(pointer({ behind: true }))} />)
    expect(screen.getByRole('button', { name: 'Abandon' })).toBeInTheDocument()
  })

  it('Abandon opens a typed-slug confirm and only mutates once the slug matches', () => {
    render(<MapChangesCard product={product(pointer())} />)
    fireEvent.click(screen.getByRole('button', { name: 'Abandon' }))
    const dialog = within(screen.getByRole('dialog'))
    const input = dialog.getByLabelText('Type kindash to confirm')
    const dialogConfirm = dialog.getByRole('button', { name: 'Abandon' })
    expect(dialogConfirm).toBeDisabled()
    expect(h.abandon).not.toHaveBeenCalled()
    fireEvent.change(input, { target: { value: 'kindash' } })
    expect(dialogConfirm).toBeEnabled()
    fireEvent.click(dialogConfirm)
    expect(h.abandon).toHaveBeenCalled()
  })

  it('Abandon dialog is busy while the mutation is pending', () => {
    Object.assign(h.abandonState, { isPending: true })
    render(<MapChangesCard product={product(pointer())} />)
    fireEvent.click(screen.getByRole('button', { name: 'Abandon' }))
    const dialogConfirm = within(screen.getByRole('dialog')).getByRole('button', { name: 'Abandon' })
    expect(dialogConfirm).toBeDisabled()
    expect(dialogConfirm).toHaveAttribute('aria-busy', 'true')
  })
})
