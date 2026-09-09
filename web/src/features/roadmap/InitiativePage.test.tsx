// `/initiatives/:initiativeSlug` — the initiative level: what the breadcrumb
// says it belongs to, why it needs a human, what each tab answers, and that the
// human gate is a two-step (open the confirm, then confirm) rather than a tap.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
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
    advanceIsError: false,
  }
  return { advanceMutate, initiative, map, state }
})

vi.mock('@/api/hooks', () => ({
  useInitiative: () => ({ data: h.state.data, isLoading: h.state.isLoading, isError: h.state.isError }),
  useRoadmapMap: () => ({ data: h.map }),
  useAdvanceInitiative: () => ({
    mutate: h.advanceMutate,
    isPending: false,
    isError: h.state.advanceIsError,
    error: null,
  }),
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
    h.state.advanceIsError = false
  })

  it('shows the breadcrumb, the reason, the next action and the artifact link at its SHA', () => {
    mount()
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Use-case-first task views')
    expect(screen.getByRole('link', { name: 'kindash' })).toHaveAttribute('href', '/roadmap/kindash')
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

  it('says a failed advance out loud, in place', () => {
    h.state.advanceIsError = true
    mount()
    expect(screen.getByRole('alert')).toHaveTextContent('Could not advance initiative')
  })

  it('a failed fetch is a banner, first thing in the content region', () => {
    h.state.data = undefined
    h.state.isError = true
    mount()
    expect(screen.getByRole('alert')).toHaveTextContent('Could not load this initiative.')
  })

  it('an initiative that is not there says so, and offers the way back', () => {
    h.state.data = undefined
    mount('/initiatives/ghost')
    expect(screen.getByText('Initiative not found')).toBeInTheDocument()
    // Back to the list this initiative would have been a row of, not to the map.
    expect(screen.getByRole('link', { name: /Back to the roadmap/i })).toHaveAttribute('href', '/roadmap?scope=initiatives')
    // Resolved-and-empty is not "still loading": the region must not sit busy.
    expect(screen.getByTestId('page-content')).not.toHaveAttribute('aria-busy')
  })
})
