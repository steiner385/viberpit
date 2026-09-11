// The decomposition gate on the Track tab (spec 003 §7b, #992): what the plan
// would become on GitHub — one epic and its chained task sub-issues — shown
// before a single issue exists, with the two ways out.
//
// The card's whole contract is about WHEN it is on screen: only for a `pending`
// proposal, gone the moment the decision lands, and still there after a failed
// accept, because that apply is resumable and a second tap continues it rather
// than creating a second epic.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { ApiRequestError } from '@/api/client'
import { InitiativePage } from './InitiativePage'

const h = vi.hoisted(() => {
  const acceptMutate = vi.fn()
  const rejectMutate = vi.fn()
  // `dependsOn` holds INDEXES into this same array (server `depends_on`) — the
  // epic does not exist yet, so there are no issue numbers to point at.
  const proposal = {
    id: 'mp1',
    initiativeId: 'i1',
    status: 'pending',
    sessionId: 's9',
    rationale: 'Three phases, each one PR.',
    epicTitle: 'Epic: use-case-first task views',
    epicBody: 'What the plan buys.',
    tasks: [
      { title: 'Phase A — schema', body: 'a', dependsOn: [] as number[] },
      { title: 'Phase B — endpoint', body: 'b', dependsOn: [0] },
      { title: 'Phase C — view', body: 'c', dependsOn: [0, 1] },
    ],
    created: null,
    createdAt: null,
    resolvedAt: null,
  }
  const initiative = {
    slug: 'views', repo: 'cairnea/KinDash', title: 'Use-case-first task views',
    stage: 'plan', status: 'active', origin: 'human', originRef: null,
    epicRepo: null, epicNumber: null, createdAt: null, updatedAt: null,
    artifacts: [],
    epic: null,
    projectSlug: 'kindash',
    primaryCapability: null,
    capabilities: [],
    buildSessions: [],
    nextAction: { type: 'materialize', kind: 'plan' },
  }
  const state = {
    proposal: proposal as Record<string, unknown> | null,
    acceptIsError: false,
    acceptError: null as unknown,
    rejectIsError: false,
    rejectError: null as unknown,
  }
  return { acceptMutate, rejectMutate, proposal, initiative, state }
})

vi.mock('@/api/hooks', () => ({
  useInitiative: () => ({ data: h.initiative, isLoading: false, isError: false, error: null }),
  useRoadmapMap: () => ({ data: undefined }),
  useAdvanceInitiative: () => ({ mutate: vi.fn(), isPending: false, isError: false, error: null }),
  useMaterialization: () => ({ data: h.state.proposal }),
  useAcceptMaterialization: () => ({
    mutate: h.acceptMutate, isPending: false,
    isError: h.state.acceptIsError, error: h.state.acceptError,
  }),
  useRejectMaterialization: () => ({
    mutate: h.rejectMutate, isPending: false,
    isError: h.state.rejectIsError, error: h.state.rejectError,
  }),
}))

// A FUNCTION, not a shared element: `rerender` with the identical element
// object lets React bail out of the subtree, and these tests change what the
// mocked hooks answer rather than any prop — a fresh element is what makes the
// re-render actually happen.
const page = () => (
  <MemoryRouter initialEntries={['/initiatives/views']}>
    <Routes><Route path="/initiatives/:initiativeSlug" element={<InitiativePage />} /></Routes>
  </MemoryRouter>
)

const mount = () => render(page())
const card = () => screen.queryByRole('region', { name: /materialize plan/i })

describe('InitiativePage — materialization gate', () => {
  beforeEach(() => {
    h.acceptMutate.mockClear()
    h.rejectMutate.mockClear()
    h.state.proposal = h.proposal
    h.state.acceptIsError = false
    h.state.acceptError = null
    h.state.rejectIsError = false
    h.state.rejectError = null
  })

  it('shows the proposed epic, every task, and what each task waits on', () => {
    mount()
    expect(screen.getByText('Epic: use-case-first task views')).toBeInTheDocument()
    expect(screen.getByText('Three phases, each one PR.')).toBeInTheDocument()
    const tasks = within(screen.getByRole('list', { name: /proposed tasks/i })).getAllByRole('listitem')
    expect(tasks).toHaveLength(3)
    expect(tasks[0]).toHaveTextContent('Phase A — schema')
    // Index-based, exactly as the API states them: nothing is on GitHub yet.
    expect(within(tasks[1]!).getByText('after #0')).toBeInTheDocument()
    expect(within(tasks[2]!).getByText('after #0, #1')).toBeInTheDocument()
    // The first task waits on nothing, and says nothing rather than "after".
    expect(within(tasks[0]!).queryByText(/after/)).not.toBeInTheDocument()
  })

  it('the next action names the decision the card is asking for', () => {
    mount()
    expect(screen.getByText(/decompose/i)).toBeInTheDocument()
  })

  it('Accept applies the proposal, and the card goes once the decision lands', () => {
    const { rerender } = mount()
    fireEvent.click(screen.getByRole('button', { name: 'Accept' }))
    expect(h.acceptMutate).toHaveBeenCalledWith('views')
    // The refetch the mutation's invalidation triggers: same proposal, decided.
    h.state.proposal = { ...h.proposal, status: 'accepted' }
    rerender(page())
    expect(card()).not.toBeInTheDocument()
  })

  it('Reject turns the decomposition down, and the card goes with it', () => {
    const { rerender } = mount()
    fireEvent.click(screen.getByRole('button', { name: 'Reject' }))
    expect(h.rejectMutate).toHaveBeenCalledWith('views')
    h.state.proposal = { ...h.proposal, status: 'rejected' }
    rerender(page())
    expect(card()).not.toBeInTheDocument()
  })

  it('a failed accept says why, in place, and keeps the card for a re-tap', () => {
    h.state.acceptIsError = true
    h.state.acceptError = new ApiRequestError(502, {
      detail: { error: 'materialize_failed', step: 'task', message: 'createIssue returned no issue' },
    })
    mount()
    expect(screen.getByRole('alert')).toHaveTextContent('createIssue returned no issue')
    expect(card()).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Accept' })).toBeEnabled()
  })

  it('an initiative that was never decomposed shows no card at all', () => {
    h.state.proposal = null
    mount()
    expect(card()).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Accept' })).not.toBeInTheDocument()
  })
})
