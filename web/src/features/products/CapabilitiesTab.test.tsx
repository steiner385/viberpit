import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { CapabilitiesTab } from './CapabilitiesTab'
import { ApiRequestError } from '@/api/client'
import type { CapabilityNode } from '@/api/roadmap'

const retireAsync = vi.fn()
const mergeAsync = vi.fn()
const state = { isPending: false }

vi.mock('@/api/hooks', () => {
  const retireFn = vi.fn()
  const mergeFn = vi.fn()
  retireFn.mockImplementation(() => ({ mutateAsync: retireAsync, ...state }))
  mergeFn.mockImplementation(() => ({ mutateAsync: mergeAsync, ...state }))
  return {
    useRetireCapability: retireFn,
    useMergeCapabilities: mergeFn,
  }
})

const CAPS: CapabilityNode[] = [
  {
    id: 'f1', slug: 'sessions', title: 'Sessions', state: 'live', summary: null,
    docPath: null, commitSha: 'abc', prNumber: null, parentSlug: null, tier: 'live',
    counts: { openIssues: 3, inFlight: 1, blocked: 0, needsYou: 0 }, links: [], initiatives: [], personaSlugs: [],
    children: [
      {
        id: 'f2', slug: 'dictation', title: 'Dictation', state: 'delivering', summary: null,
        docPath: null, commitSha: 'def', prNumber: null, parentSlug: 'sessions', tier: 'in-flight',
        counts: { openIssues: 5, inFlight: 2, blocked: 1, needsYou: 0 }, links: [], initiatives: [], personaSlugs: [],
        children: [],
      },
    ],
  },
]

// A third, already-retired capability — neither retirable again nor a place to
// fold live work into.
const SUNSET: CapabilityNode = {
  id: 'f3', slug: 'legacy', title: 'Legacy', state: 'sunset', summary: null,
  docPath: null, commitSha: 'ghi', prNumber: null, parentSlug: null, tier: 'live',
  counts: { openIssues: 0, inFlight: 0, blocked: 0, needsYou: 0 }, links: [], initiatives: [], personaSlugs: [],
  children: [],
}
const CAPS_WITH_SUNSET: CapabilityNode[] = [...CAPS, SUNSET]

const PRODUCT = {
  slug: 'kindash',
  capabilities: CAPS,
} as never

const PRODUCT_WITH_SUNSET = {
  slug: 'kindash',
  capabilities: CAPS_WITH_SUNSET,
} as never

describe('CapabilitiesTab', () => {
  beforeEach(() => {
    retireAsync.mockReset()
    mergeAsync.mockReset()
    state.isPending = false
  })

  it('lists every capability with its state and open-issue count', () => {
    render(<CapabilitiesTab product={PRODUCT} capabilities={CAPS} />)
    expect(screen.getByText('Sessions')).toBeInTheDocument()
    expect(screen.getByText('Dictation')).toBeInTheDocument()
    expect(screen.getByText('Live')).toBeInTheDocument()
    expect(screen.getByText('Delivering')).toBeInTheDocument()
    expect(screen.getByText('3')).toBeInTheDocument()
    expect(screen.getByText('5')).toBeInTheDocument()
    expect(screen.getByText('Open issues')).toBeInTheDocument()
  })

  it('retiring a capability with dependents shows the server refusal', async () => {
    retireAsync.mockRejectedValue(
      new ApiRequestError(422, { detail: { error: 'has_dependents', message: 'nope' } }),
    )
    render(<CapabilitiesTab product={PRODUCT} capabilities={CAPS} />)
    fireEvent.click(screen.getAllByRole('button', { name: /^retire$/i })[1]) // dictation
    fireEvent.click(screen.getByRole('button', { name: 'Retire Dictation' }))
    await waitFor(() => expect(retireAsync).toHaveBeenCalledWith({ slug: 'dictation' }))
    // The refusal answers the tap that is still on screen: inside the dialog,
    // which stays open; Cancel takes the question and the refusal away together.
    const dialog = await screen.findByRole('dialog', { name: 'Retire Dictation?' })
    expect(await screen.findByRole('alert')).toHaveTextContent(/nope/i)
    expect(dialog).toContainElement(screen.getByRole('alert'))
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('retiring closes the dialog once the server accepts', async () => {
    retireAsync.mockResolvedValue({ pointer: {} })
    render(<CapabilitiesTab product={PRODUCT} capabilities={CAPS} />)
    fireEvent.click(screen.getAllByRole('button', { name: /^retire$/i })[1]) // dictation
    fireEvent.click(screen.getByRole('button', { name: 'Retire Dictation' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('merging a capability shows the repointed counts from the response', async () => {
    mergeAsync.mockResolvedValue({
      pointer: { branch: 'pm/map-changes-kindash', prNumber: 9, prUrl: null, status: 'open',
                 openedAt: null, headSha: 'x', behind: false, conflicting: false },
      workItems: 0,
      initiatives: 0,
      linksRepointed: 1,
      linksNotRepointed: 2,
      childrenReparented: 0,
    })
    render(<CapabilitiesTab product={PRODUCT} capabilities={CAPS} />)
    fireEvent.click(screen.getAllByRole('button', { name: /merge into/i })[1]) // dictation
    fireEvent.change(screen.getByRole('combobox', { name: /merge dictation into/i }), {
      target: { value: 'sessions' },
    })
    fireEvent.click(screen.getByRole('button', { name: /confirm merge/i }))
    await waitFor(() => expect(mergeAsync).toHaveBeenCalledWith({
      fromSlug: 'dictation', intoSlug: 'sessions',
    }))
    expect(await screen.findByText(/2 cross-product link/i)).toBeInTheDocument()
  })

  // 2026-09-30 Roadmap critique, P2: the old inline confirm was a 12px sentence
  // that named neither the open issues two cells to its left nor which children
  // move where. The gate is a Dialog and names every consequence.
  it('retiring opens a dialog that names the children, where they go, and the open issues', () => {
    render(<CapabilitiesTab product={PRODUCT} capabilities={CAPS} />)
    fireEvent.click(screen.getAllByRole('button', { name: /^retire$/i })[0]) // sessions
    const dialog = screen.getByRole('dialog', { name: 'Retire Sessions?' })
    expect(dialog).toHaveTextContent('Sessions is marked Sunset in its feature doc and leaves the tree')
    expect(dialog).toHaveTextContent('1 sub-capability (Dictation) moves up to the top level')
    expect(dialog).toHaveTextContent('3 open issues counted under it stay where they are')
    expect(dialog).toHaveTextContent(/if any is mapped directly to it, the retire is refused/)
    expect(dialog).toHaveTextContent(/Reverse: Not from here/)
    // The doc commit is a GitHub write, badged like every other gate's.
    expect(screen.getAllByText('writes to GitHub')).toHaveLength(2)
    expect(screen.getByRole('button', { name: 'Retire Sessions' })).toBeInTheDocument()
    expect(retireAsync).not.toHaveBeenCalled()
  })

  it('a leaf under a parent says it has no children to move, and names its parent otherwise', () => {
    render(<CapabilitiesTab product={PRODUCT} capabilities={CAPS} />)
    fireEvent.click(screen.getAllByRole('button', { name: /^retire$/i })[1]) // dictation
    const dialog = screen.getByRole('dialog', { name: 'Retire Dictation?' })
    expect(dialog).not.toHaveTextContent(/sub-capabilit/)
    expect(dialog).toHaveTextContent('5 open issues counted under it')
  })

  it('offers neither Retire nor a merge target for an already-sunset capability', () => {
    render(<CapabilitiesTab product={PRODUCT_WITH_SUNSET} capabilities={CAPS_WITH_SUNSET} />)
    // Three rows, but only the two live ones can be retired.
    expect(screen.getByText('Legacy')).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /^retire$/i })).toHaveLength(2)

    // …and `legacy` is not offered as somewhere to fold `dictation` into.
    fireEvent.click(screen.getAllByRole('button', { name: /merge into/i })[1]) // dictation
    const targets = Array.from(
      screen.getByRole('combobox', { name: /merge dictation into/i })
        .querySelectorAll('option'),
    ).map((o) => o.value)
    expect(targets).toEqual(['', 'sessions'])
  })
})
