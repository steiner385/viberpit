import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { CapabilitiesTab } from './CapabilitiesTab'
import { ApiRequestError } from '@/api/client'
import type { CapabilityNode } from '@/api/roadmap'

const mutateAsync = vi.fn()
const state = { isPending: false }

vi.mock('@/api/hooks', () => ({
  useRetireCapability: () => ({ mutateAsync, ...state }),
}))

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

describe('CapabilitiesTab', () => {
  beforeEach(() => {
    mutateAsync.mockReset()
    state.isPending = false
  })

  it('lists every capability with its state and open-issue count', () => {
    render(<CapabilitiesTab product={{ slug: 'kindash' } as never} capabilities={CAPS} />)
    expect(screen.getByText('Sessions')).toBeInTheDocument()
    expect(screen.getByText('Dictation')).toBeInTheDocument()
    expect(screen.getByText('Live')).toBeInTheDocument()
    expect(screen.getByText('Delivering')).toBeInTheDocument()
    expect(screen.getByText('3')).toBeInTheDocument()
    expect(screen.getByText('5')).toBeInTheDocument()
    expect(screen.getByText('Open issues')).toBeInTheDocument()
  })

  it('retiring a capability with dependents shows the server refusal', async () => {
    mutateAsync.mockRejectedValue(
      new ApiRequestError(422, { detail: { error: 'has_dependents', message: 'nope' } }),
    )
    render(<CapabilitiesTab product={{ slug: 'kindash' } as never} capabilities={CAPS} />)
    fireEvent.click(screen.getAllByRole('button', { name: /retire/i })[1]) // dictation
    fireEvent.click(screen.getByRole('button', { name: /confirm/i }))
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledWith({ slug: 'dictation' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/nope/i)
  })
})
