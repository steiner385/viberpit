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
import { BRAND_NAME } from '@/brand'
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
    guidance: null as Record<string, unknown> | null,
  }
  const state = {
    proposal: proposal as Record<string, unknown> | null,
    acceptIsError: false,
    acceptError: null as unknown,
    acceptIsPending: false,
    rejectIsError: false,
    rejectError: null as unknown,
    rejectIsPending: false,
  }
  return { acceptMutate, rejectMutate, proposal, initiative, state }
})

vi.mock('@/api/hooks', () => ({
  useInitiative: () => ({ data: h.initiative, isLoading: false, isError: false, error: null }),
  useRoadmapMap: () => ({ data: undefined }),
  useAdvanceInitiative: () => ({ mutate: vi.fn(), isPending: false, isError: false, error: null }),
  useMaterialization: () => ({ data: h.state.proposal }),
  useAcceptMaterialization: () => ({
    mutate: h.acceptMutate, isPending: h.state.acceptIsPending,
    isError: h.state.acceptIsError, error: h.state.acceptError,
  }),
  useRejectMaterialization: () => ({
    mutate: h.rejectMutate, isPending: h.state.rejectIsPending,
    isError: h.state.rejectIsError, error: h.state.rejectError,
  }),
  useLeaves: () => ({ data: [], isLoading: false, isError: false }),
  useRemoveDependency: () => ({ mutate: vi.fn(), isPending: false, isError: false, error: null }),
  useStepBackInitiative: () => ({ mutate: vi.fn(), isPending: false, isError: false, error: null }),
  useUnblockInitiative: () => ({ mutate: vi.fn(), isPending: false, isError: false, error: null }),
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

// The hoisted `h.initiative` is what the mocked `useInitiative` answers, and a
// test that needs guidance REPLACES it. Restoring in the body leaks on a failed
// assertion, so the reset is a `beforeEach` over this snapshot instead.
const baseInitiative = { ...h.initiative }

describe('InitiativePage — materialization gate', () => {
  beforeEach(() => {
    h.acceptMutate.mockClear()
    h.rejectMutate.mockClear()
    h.initiative = { ...baseInitiative }
    h.state.proposal = h.proposal
    h.state.acceptIsError = false
    h.state.acceptError = null
    h.state.acceptIsPending = false
    h.state.rejectIsError = false
    h.state.rejectError = null
    h.state.rejectIsPending = false
  })

  it('shows the proposed epic, every task, and what each task waits on', () => {
    mount()
    expect(screen.getByText('Epic: use-case-first task views')).toBeInTheDocument()
    expect(screen.getByText('Three phases, each one PR.')).toBeInTheDocument()
    const tasks = within(screen.getByRole('list', { name: /proposed tasks/i })).getAllByRole('listitem')
    expect(tasks).toHaveLength(3)
    expect(tasks[0]).toHaveTextContent('Phase A — schema')
    // 1-BASED on screen while `dependsOn` stays 0-based on the wire: the row
    // labels and the references have to count the same way for a human.
    expect(within(tasks[0]!).getByText('Task 1')).toBeInTheDocument()
    expect(within(tasks[1]!).getByText('after task 1')).toBeInTheDocument()
    expect(within(tasks[2]!).getByText('after tasks 1, 2')).toBeInTheDocument()
    // The first task waits on nothing, and says nothing rather than "after".
    expect(within(tasks[0]!).queryByText(/after/)).not.toBeInTheDocument()
  })

  it('Accept confirms first — like Advance — and says what it will create', () => {
    mount()
    fireEvent.click(screen.getByRole('button', { name: 'Accept' }))
    // Nothing has been written yet: the tap opened the gate, it did not pass it.
    expect(h.acceptMutate).not.toHaveBeenCalled()
    const dialog = screen.getByRole('dialog')
    expect(dialog).toHaveTextContent('3 sub-issues')
    expect(dialog).toHaveTextContent(`cannot be undone from ${BRAND_NAME}`)
    // This confirm is ALSO the plan stage's gate: the apply advances the
    // initiative, so no Advance tap follows and the copy has to say so.
    expect(dialog).toHaveTextContent('moves this initiative to Build')
  })

  it('the Materialize confirm lists the computed consequence when guidance is present', () => {
    const accept = {
      available: true, unavailableReason: null,
      immediate: [
        { text: 'Creates the epic and 2 sub-issues on GitHub; this cannot be undone here.', spawnsSession: false, writesGithub: true },
        { text: 'Moves the gate to build.', spawnsSession: false, writesGithub: false },
      ],
      then: [{ text: 'Nothing more from the roadmap loop.', spawnsSession: false, writesGithub: false }],
      when: 'now', reversal: 'Close the created issues', gate: 'materialization.accept',
    }
    const reject = { ...accept, immediate: [{ text: 'Rejects this decomposition; nothing is created on GitHub.', spawnsSession: false, writesGithub: false }],
      then: [{ text: 'Spawns a decompose session that proposes an epic and sub-issues for you to accept or reject.', spawnsSession: true, writesGithub: false }],
      when: 'next_tick', reversal: null, gate: null }
    h.initiative = { ...h.initiative, guidance: { machine: null, tickSeconds: 60,
      tolerance: { effective: 'cautious', configured: 'cautious', autoresolveEnabled: true, synthesizerMode: 'on' },
      actions: { advance: accept, step_back: accept, unblock: accept, materialize_accept: accept, materialize_reject: reject } } }
    mount()
    // Reject renders through `ConsequenceList` like everything else: a heading,
    // the step text, and the cost badge — no hand-formatted "Reject: …" line.
    // "Reject" is also the button's label, so the heading is the non-button one.
    expect(screen.getAllByText('Reject').some((el) => el.tagName !== 'BUTTON')).toBe(true)
    expect(screen.getByText(/^Spawns a decompose session/)).toBeInTheDocument()
    expect(screen.getByText('starts a session')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Accept' }))
    expect(screen.getByText('writes to GitHub')).toBeInTheDocument()
    expect(screen.getByText('Reverse: Close the created issues')).toBeInTheDocument()
  })

  it('a pending proposal with issues behind it reads as a resume', () => {
    // A pending proposal carrying `created` is a FAILED apply waiting to continue
    // — Accept resumes from those issues rather than creating a second set.
    h.state.proposal = {
      ...h.proposal,
      created: {
        repository_id: 'R_1',
        epic: { node_id: 'I_1', number: 101 },
        tasks: { '0': { node_id: 'I_2', number: 102 } },
      },
    }
    mount()
    expect(screen.getByText('2 issues already created — Accept resumes.')).toBeInTheDocument()
  })

  it('says nothing about resuming when only the repository id was remembered', () => {
    // `repository_id` is a cached constant, not an issue: counting it would tell a
    // human an issue exists that does not.
    h.state.proposal = { ...h.proposal, created: { repository_id: 'R_1' } }
    mount()
    expect(screen.queryByText(/already created/)).not.toBeInTheDocument()
  })

  it('cancelling the confirm writes nothing and leaves the card up', () => {
    mount()
    fireEvent.click(screen.getByRole('button', { name: 'Accept' }))
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(h.acceptMutate).not.toHaveBeenCalled()
    expect(card()).toBeInTheDocument()
  })

  it('confirming applies the proposal, and the card goes once the decision lands', () => {
    const { rerender } = mount()
    fireEvent.click(screen.getByRole('button', { name: 'Accept' }))
    fireEvent.click(screen.getByRole('button', { name: 'Confirm materialize' }))
    expect(h.acceptMutate).toHaveBeenCalledWith('views', expect.anything())
    // The refetch the mutation's invalidation triggers: same proposal, decided.
    h.state.proposal = { ...h.proposal, status: 'accepted' }
    rerender(page())
    expect(card()).not.toBeInTheDocument()
  })

  it('either decision in flight disables both controls', () => {
    h.state.rejectIsPending = true
    const { rerender } = mount()
    expect(screen.getByRole('button', { name: 'Accept' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Reject' })).toBeDisabled()
    h.state.rejectIsPending = false
    h.state.acceptIsPending = true
    rerender(page())
    expect(screen.getByRole('button', { name: 'Accept' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Reject' })).toBeDisabled()
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
