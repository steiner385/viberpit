// `/initiatives/:initiativeSlug` — the initiative level: what the breadcrumb
// says it belongs to, why it needs a human, what each tab answers, and that the
// human gate is a two-step (open the confirm, then confirm) rather than a tap.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { ApiRequestError } from '@/api/client'
import { InitiativePage } from './InitiativePage'

// Everything the mocked hooks answer with, in one hoisted bag: the mock factory
// runs before the module body, so a test that needs the query to fail (or to
// resolve empty) mutates THIS rather than re-mocking per file.
const h = vi.hoisted(() => {
  const advanceMutate = vi.fn()
  const initiative = {
    slug: 'views', repo: 'cairnea/KinDash', title: 'Use-case-first task views',
    stage: 'build', status: 'active', origin: 'human', originRef: null,
    epicRepo: 'cairnea/KinDash', epicNumber: 15727, createdAt: null, updatedAt: null,
    artifacts: [{
      id: 'a1', kind: 'spec', repoPath: 'docs/spec.md', commitSha: 'abc1234',
      prNumber: null, prUrl: null, sessionId: null, model: null, createdAt: null,
    }],
    epic: {
      ref: 'cairnea/KinDash#15727', url: 'https://github.com/cairnea/KinDash/issues/15727',
      total: 2, done: 2, blocked: 0, needsHuman: 0,
      children: [
        { ref: 'cairnea/KinDash#1', title: 'Child one', state: 'closed', status: 'Done' },
        { ref: 'cairnea/KinDash#2', title: 'Child two', state: 'closed', status: 'Done' },
      ],
    },
    projectSlug: 'kindash',
    primaryCapability: { id: 'f1', slug: 'task-views', title: 'Task views' },
    capabilities: [{ id: 'f1', slug: 'task-views', title: 'Task views', isPrimary: true }],
    buildSessions: [{ id: 's1', status: 'complete', branch: 'ccswitch/views' }],
    nextAction: { type: 'handoff_to_build' },
  }
  // The map is only read for the initiative node's `reason` — the shipped
  // fixture's `views` has none, and "needs a human, because it is ready to
  // advance" is the state this page exists to show.
  const map = {
    generatedAt: 'now',
    products: [{
      slug: 'kindash', name: 'KinDash', tier: 'needs-you',
      counts: { inFlight: 0, blocked: 0, needsYou: 1, capabilities: 1, awaitingTriage: 0, needsHome: 0 },
      capabilities: [],
      initiatives: [{
        slug: 'views', title: 'Use-case-first task views', stage: 'build', status: 'active',
        projectSlug: 'kindash', primaryCapability: 'task-views', capabilities: ['task-views'],
        tier: 'needs-you', reason: 'advance', epic: null,
      }],
    }],
  }
  const state = {
    data: initiative as Record<string, unknown> | undefined,
    isLoading: false,
    isError: false,
    error: null as unknown,
    map: map as unknown,
    advanceIsError: false,
  }
  return { advanceMutate, initiative, map, state }
})

vi.mock('@/api/hooks', () => ({
  useInitiative: () => ({
    data: h.state.data,
    isLoading: h.state.isLoading,
    isError: h.state.isError,
    error: h.state.error,
  }),
  useRoadmapMap: () => ({ data: h.state.map }),
  useAdvanceInitiative: () => ({
    mutate: h.advanceMutate,
    isPending: false,
    isError: h.state.advanceIsError,
    error: null,
  }),
  // The Track tab's materialization gate. Nothing to decide here — the card
  // has its own file (`InitiativePage.materialization.test.tsx`); these three
  // only have to exist, because the page mounts them unconditionally.
  useMaterialization: () => ({ data: null }),
  useAcceptMaterialization: () => ({ mutate: vi.fn(), isPending: false, isError: false, error: null }),
  useRejectMaterialization: () => ({ mutate: vi.fn(), isPending: false, isError: false, error: null }),
}))

function mount(initial = '/initiatives/views') {
  render(
    <MemoryRouter initialEntries={[initial]}>
      <Routes><Route path="/initiatives/:initiativeSlug" element={<InitiativePage />} /></Routes>
    </MemoryRouter>,
  )
}

describe('InitiativePage', () => {
  beforeEach(() => {
    h.advanceMutate.mockClear()
    h.state.data = h.initiative
    h.state.isLoading = false
    h.state.isError = false
    h.state.error = null
    h.state.map = h.map
    h.state.advanceIsError = false
  })

  it('shows the breadcrumb, the reason, the next action and the artifact link at its SHA', () => {
    mount()
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Use-case-first task views')
    // The product crumb is the map's display NAME, not the slug the payload carries.
    expect(screen.getByRole('link', { name: 'KinDash' })).toHaveAttribute('href', '/roadmap/kindash')
    expect(screen.getByText('Ready to advance')).toBeInTheDocument()
    expect(screen.getByText('Ready to hand off to build.')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /spec/ })).toHaveAttribute('href', expect.stringContaining('abc1234'))
  })

  it('advances only after confirming', () => {
    mount()
    fireEvent.click(screen.getByRole('button', { name: /Advance → Deploy/ }))
    expect(h.advanceMutate).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Confirm advance' }))
    expect(h.advanceMutate).toHaveBeenCalledWith('views', expect.anything())
  })

  it('cancelling the confirm leaves the gate where it was', () => {
    mount()
    fireEvent.click(screen.getByRole('button', { name: /Advance → Deploy/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(h.advanceMutate).not.toHaveBeenCalled()
  })

  it('offers nothing to advance at the final stage', () => {
    h.state.data = { ...h.initiative, stage: 'done', status: 'done' }
    mount()
    expect(screen.getByRole('button', { name: 'Complete' })).toBeDisabled()
  })

  it('Epic tab lists children; Sessions tab links to the session; Capabilities tab links to the capability', () => {
    mount('/initiatives/views?tab=epic')
    expect(screen.getByText('Child one')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('tab', { name: /Sessions/ }))
    expect(screen.getByRole('link', { name: /ccswitch\/views/ })).toHaveAttribute('href', '/sessions/s1')
    fireEvent.click(screen.getByRole('tab', { name: /Capabilities/ }))
    expect(screen.getByRole('link', { name: /Task views/ })).toHaveAttribute('href', '/roadmap/kindash/capabilities/task-views')
  })

  it('links the latest commit, not the first one committed', () => {
    // The server sends artifacts oldest first, so the newest committed SHA is
    // the LAST one — this used to link the intent commit.
    h.state.data = {
      ...h.initiative,
      artifacts: [
        { ...h.initiative.artifacts[0]!, id: 'a0', kind: 'intent', repoPath: 'docs/intent.md', commitSha: 'aaa0000' },
        { ...h.initiative.artifacts[0]!, id: 'a1', kind: 'spec', repoPath: 'docs/spec.md', commitSha: 'bbb1111' },
      ],
    }
    mount()
    expect(screen.getByRole('link', { name: /latest commit/ }))
      .toHaveAttribute('href', 'https://github.com/cairnea/KinDash/commit/bbb1111')
  })

  it('says a failed advance out loud, in place', () => {
    h.state.advanceIsError = true
    mount()
    expect(screen.getByRole('alert')).toHaveTextContent('Could not advance initiative')
  })

  it('a failed fetch is a banner, first thing in the content region', () => {
    h.state.data = undefined
    h.state.isError = true
    h.state.error = new ApiRequestError(500, {})
    mount()
    expect(screen.getByRole('alert')).toHaveTextContent('Could not load this initiative.')
    expect(screen.queryByText('Initiative not found')).not.toBeInTheDocument()
  })

  it('a 404 is a missing address, not a broken connection', () => {
    // The endpoint answers 404 and `shouldRetryQuery` fails fast, so this is the
    // ONLY state a stale bookmark can reach — there is no "resolved empty" one.
    h.state.data = undefined
    h.state.isError = true
    h.state.error = new ApiRequestError(404, { detail: 'not found' })
    mount('/initiatives/ghost')
    expect(screen.getByText('Initiative not found')).toBeInTheDocument()
    // Back to the list this initiative would have been a row of, not to the map.
    expect(screen.getByRole('link', { name: /Back to the roadmap/i })).toHaveAttribute('href', '/roadmap?scope=initiatives')
    // Not a failure to report: the address is simply not one.
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    // Not "still loading" either: the region must not sit busy.
    expect(screen.getByTestId('page-content')).not.toHaveAttribute('aria-busy')
  })

  it('the map having loaded without the slug says not found too', () => {
    h.state.data = undefined
    mount('/initiatives/ghost')
    expect(screen.getByText('Initiative not found')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /Back to the roadmap/i })).toBeInTheDocument()
  })

  it('waits for the map before calling a slug missing', () => {
    h.state.data = undefined
    h.state.map = undefined
    h.state.isLoading = true
    mount('/initiatives/ghost')
    expect(screen.queryByText('Initiative not found')).not.toBeInTheDocument()
    expect(screen.getByTestId('page-content')).toHaveAttribute('aria-busy', 'true')
  })
})
