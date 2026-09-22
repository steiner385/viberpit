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
    counts: { openIssues: 3, inFlight: 1, blocked: 0, needsYou: 0 }, links: [], initiatives: [],
    children: [
      {
        id: 'f2', slug: 'dictation', title: 'Dictation', state: 'delivering', summary: null,
        docPath: null, commitSha: 'def', prNumber: null, parentSlug: 'sessions', tier: 'in-flight',
        counts: { openIssues: 5, inFlight: 2, blocked: 1, needsYou: 0 }, links: [], initiatives: [],
        children: [],
      },
    ],
  },
]

const PRODUCT = {
  slug: 'kindash',
  capabilities: CAPS,
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
    fireEvent.click(screen.getAllByRole('button', { name: /retire/i })[1]) // dictation
    fireEvent.click(screen.getByRole('button', { name: /confirm/i }))
    await waitFor(() => expect(retireAsync).toHaveBeenCalledWith({ slug: 'dictation' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/nope/i)
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
})
