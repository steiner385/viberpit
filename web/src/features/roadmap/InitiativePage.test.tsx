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
  const stepBackMutate = vi.fn()
  const unblockMutate = vi.fn()
  const updateMutate = vi.fn()
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
    // A plain (non-program, non-phase) initiative: no parent, no children.
    parentInitiativeSlug: null,
    hasChildren: false,
    children: [] as { slug: string; title: string | null; stage: string | null; status: string | null }[],
    buildSessions: [{ id: 's1', status: 'complete', branch: 'viberpit/views' }],
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
      capabilities: [{
        id: 'f1', slug: 'task-views', title: 'Task views', state: 'live', summary: null, docPath: null,
        commitSha: null, prNumber: null, parentSlug: null, tier: 'idle',
        counts: { openIssues: 0, inFlight: 0, blocked: 0, needsYou: 0 },
        links: [], initiatives: [], children: [],
      }],
      initiatives: [{
        slug: 'views', title: 'Use-case-first task views', stage: 'build', status: 'active',
        projectSlug: 'kindash', primaryCapability: 'task-views', capabilities: ['task-views'],
        tier: 'needs-you', reason: 'advance', epic: null,
        parentInitiative: null, hasChildren: false,
      }, {
        // The program a "Part of" test points `views` at — kept off `views`
        // itself (`parentInitiativeSlug` stays null there) so only the one
        // test that mutates it exercises the link.
        slug: 'audit', title: 'UI audit', stage: 'plan', status: 'active',
        projectSlug: 'kindash', primaryCapability: null, capabilities: [],
        tier: 'in-flight', reason: null, epic: null,
        parentInitiative: null, hasChildren: true,
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
    leaves: [] as unknown[],
    proposal: null as unknown,
  }
  return { advanceMutate, stepBackMutate, unblockMutate, updateMutate, initiative, map, state }
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
  useMaterialization: () => ({ data: h.state.proposal }),
  useAcceptMaterialization: () => ({ mutate: vi.fn(), isPending: false, isError: false, error: null }),
  useRejectMaterialization: () => ({ mutate: vi.fn(), isPending: false, isError: false, error: null }),
  useLeaves: () => ({ data: h.state.leaves, isLoading: false, isError: false }),
  useRemoveDependency: () => ({ mutate: vi.fn(), isPending: false, isError: false, error: null }),
  useUpdateOverlay: () => ({ mutate: vi.fn(), isPending: false, isError: false, error: null }),
  useStepBackInitiative: () => ({ mutate: h.stepBackMutate, isPending: false, isError: false, error: null }),
  useUnblockInitiative: () => ({ mutate: h.unblockMutate, isPending: false, isError: false, error: null }),
  useUpdateInitiative: () => ({ mutate: h.updateMutate, isPending: false, isError: false, error: null }),
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
    h.stepBackMutate.mockClear()
    h.unblockMutate.mockClear()
    h.updateMutate.mockClear()
    h.state.data = h.initiative
    h.state.isLoading = false
    h.state.isError = false
    h.state.error = null
    h.state.map = h.map
    h.state.advanceIsError = false
    h.state.leaves = []
    h.state.proposal = null
  })

  it('shows the breadcrumb, the reason, the next action and the artifact link at its SHA', () => {
    mount()
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Use-case-first task views')
    // The product crumb is the map's display NAME, not the slug the payload carries.
    expect(screen.getByRole('link', { name: 'KinDash' })).toHaveAttribute('href', '/roadmap/kindash')
    expect(screen.getByText('Ready to advance')).toBeInTheDocument()
    expect(screen.queryByText('Ready to hand off to build.')).not.toBeInTheDocument()
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

  it('names what the synthesizer did with the gate, and the control that reverses it', () => {
    // The map node carries the chip (server `auto_resolved_view`); the page
    // renders it beside the reason so "auto · advanced to plan · Step back"
    // sits next to the Step back button that undoes it.
    const map = h.map as { products: { initiatives: Record<string, unknown>[] }[] }
    h.state.map = {
      ...h.map,
      products: [{
        ...map.products[0],
        initiatives: [
          { ...map.products[0].initiatives[0],
            autoResolved: { gate: 'stage.advance', summary: 'advanced to plan', reverse: 'Step back',
              at: '2026-09-15T12:00:00+00:00', outcome: 'resolved', tolerance: 'balanced' } },
          map.products[0].initiatives[1],
        ],
      }],
    }
    mount()
    expect(screen.getByText('auto · advanced to plan · Step back')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Step back' })).toBeInTheDocument()
  })

  it('Sessions tab links to the session; Capabilities tab links to the capability', () => {
    mount('/initiatives/views?tab=sessions')
    expect(screen.getByRole('link', { name: /viberpit\/views/ })).toHaveAttribute('href', '/sessions/s1')
    fireEvent.click(screen.getByRole('tab', { name: /Capabilities/ }))
    expect(screen.getByRole('link', { name: /Task views/ })).toHaveAttribute('href', '/roadmap/kindash/capabilities/task-views')
  })

  it('Epic: renders the leaves under the epic, not the bare children list', () => {
    h.state.leaves = [{
      id: 'w1', ref: 'cairnea/KinDash#1', repo: 'cairnea/KinDash', number: 1, kind: 'issue',
      title: 'Child one', url: 'https://x/1', state: 'open', triageState: 'classified', score: 3,
      reach: null, impact: null, effort: null, confidence: null, tier: 'blocked',
      blockedBy: ['cairnea/KinDash#9'], parentRef: 'cairnea/KinDash#15727', featureId: null,
      sessionStatus: null, ghCreatedAt: null,
    }]
    mount('/initiatives/views?tab=epic')
    expect(screen.getByRole('button', { name: '#1 Child one' })).toBeInTheDocument()
    expect(screen.getByRole('list', { name: 'Blocked by' })).toBeInTheDocument()
  })

  it('a program shows a Phases tab listing its children, no Epic tab, and no Advance control', () => {
    h.state.data = {
      ...h.initiative,
      hasChildren: true,
      children: [{ slug: 'audit-01', title: '01', stage: 'plan', status: 'active' }],
    }
    mount()
    // The Epic tab is REPLACED, not joined — a program's roll-up epic has no
    // pointer of its own for a per-child list to disagree with `epic.total`.
    expect(screen.queryByRole('tab', { name: /^Epic/ })).not.toBeInTheDocument()
    expect(screen.getByRole('tab', { name: /^Phases/ })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('tab', { name: /^Phases/ }))
    expect(screen.getByText('01')).toBeInTheDocument()
    // The server 409s an advance on a program (each phase carries its own
    // gate), so the control is absent, not merely disabled.
    expect(screen.queryByRole('button', { name: /Advance|Complete/ })).not.toBeInTheDocument()
  })

  it('a program\'s Phases tab also shows the roll-up epic tiles and the leaves underneath', () => {
    // The leaves endpoint rolls a program up to its leaf descendants' issues —
    // this is what lets the Phases tab show real work, not just links to the
    // phases themselves (same StatTiles + LeafList the Epic tab renders, just
    // scoped to the program's own slug).
    h.state.data = {
      ...h.initiative,
      hasChildren: true,
      children: [{ slug: 'audit-01', title: '01', stage: 'plan', status: 'active' }],
    }
    h.state.leaves = [{
      id: 'w1', ref: 'cairnea/KinDash#1', repo: 'cairnea/KinDash', number: 1, kind: 'issue',
      title: 'Child one', url: 'https://x/1', state: 'open', triageState: 'classified', score: 3,
      reach: null, impact: null, effort: null, confidence: null, tier: 'idle',
      blockedBy: [], parentRef: 'cairnea/KinDash#15727', featureId: null,
      sessionStatus: null, ghCreatedAt: null,
    }]
    mount('/initiatives/views?tab=phases')
    // The program's own roll-up epic (`h.initiative.epic`: done 2, total 2,
    // blocked 0, needs human 0) — the same tiles `EpicTab` renders.
    expect(screen.getByText('Done')).toBeInTheDocument()
    expect(screen.getByText('Total')).toBeInTheDocument()
    expect(screen.getByText('Blocked')).toBeInTheDocument()
    expect(screen.getByText('Needs human')).toBeInTheDocument()
    // The leaves scoped to this program render below the tiles.
    expect(screen.getByRole('button', { name: '#1 Child one' })).toBeInTheDocument()
  })

  it('a phase names the program it is part of, in the header', () => {
    h.state.data = { ...h.initiative, parentInitiativeSlug: 'audit' }
    mount()
    // The map is the only place a display TITLE lives for the program's slug
    // (same reasoning `productName` already uses for the product crumb).
    expect(screen.getByRole('link', { name: 'UI audit' })).toHaveAttribute('href', '/initiatives/audit')
    expect(screen.getByText(/^Part of/)).toBeInTheDocument()
  })

  it('steps back only after confirming', () => {
    mount()
    fireEvent.click(screen.getByRole('button', { name: /Step back/ }))
    expect(h.stepBackMutate).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Confirm step back' }))
    expect(h.stepBackMutate).toHaveBeenCalledWith('views', expect.anything())
  })

  it('Step back is disabled at intake, the first stage', () => {
    h.state.data = { ...h.initiative, stage: 'intake' }
    mount()
    expect(screen.getByRole('button', { name: /Step back/ })).toBeDisabled()
  })

  it('a blocked initiative offers Unblock, disables Advance, and confirms before clearing', () => {
    h.state.data = { ...h.initiative, status: 'blocked' }
    mount()
    expect(screen.getByRole('button', { name: /Advance/ })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Unblock' }))
    expect(h.unblockMutate).not.toHaveBeenCalled()
    expect(screen.getByText(/close them first/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Confirm unblock' }))
    expect(h.unblockMutate).toHaveBeenCalledWith('views', expect.anything())
  })

  it('Unblock waits for a pending materialization to be decided', () => {
    h.state.data = { ...h.initiative, status: 'blocked' }
    h.state.proposal = { id: 'p1', status: 'pending', tasks: [], created: null }
    mount()
    expect(screen.getByRole('button', { name: 'Unblock' })).toBeDisabled()
  })

  it('an unblocked initiative has no Unblock button', () => {
    mount()
    expect(screen.queryByRole('button', { name: 'Unblock' })).not.toBeInTheDocument()
  })

  it('Edit opens the dialog seeded with the title; Save sends the changed title', () => {
    mount()
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }))
    const title = screen.getByLabelText('Title', { exact: false })
    expect(title).toHaveValue('Use-case-first task views')
    fireEvent.change(title, { target: { value: 'Views' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(h.updateMutate).toHaveBeenCalledWith({ slug: 'views', input: { title: 'Views' } }, expect.anything())
  })

  it('re-opening Edit after Cancel seeds from the initiative again, not from the abandoned draft', () => {
    // The dialog seeds once, on mount, so it has to be mounted per opening.
    mount()
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }))
    fireEvent.change(screen.getByLabelText('Title', { exact: false }), { target: { value: 'Abandoned' } })
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }))
    expect(screen.getByLabelText('Title', { exact: false })).toHaveValue('Use-case-first task views')
    expect(h.updateMutate).not.toHaveBeenCalled()
  })

  it('Edit waits for the map the dialog picks a capability out of', () => {
    h.state.map = undefined
    mount()
    expect(screen.getByRole('button', { name: 'Edit' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Edit' })).toHaveAttribute('title', 'Loading the map…')
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

  const guidance = {
    machine: {
      actor: 'you', when: 'never', summary: 'The spec is merged; tap Advance to move to plan.',
      steps: [{ text: 'The spec is merged; tap Advance to move to plan.', spawnsSession: false, writesGithub: false }],
      because: 'Risk tolerance cautious: every gate waits for you.',
    },
    actions: {
      advance: {
        available: true, unavailableReason: null,
        immediate: [{ text: 'Moves the gate to deploy.', spawnsSession: false, writesGithub: false }],
        then: [{ text: 'Deploy and maintain are carried by the pipeline and control bands, not the roadmap loop.', spawnsSession: false, writesGithub: false }],
        when: 'never', reversal: 'Step back', gate: 'stage.advance',
      },
      step_back: {
        available: true, unavailableReason: null,
        immediate: [{ text: 'Moves the gate back to plan.', spawnsSession: false, writesGithub: false }],
        then: [{ text: 'Spawns a decompose session that proposes an epic and sub-issues for you to accept or reject.', spawnsSession: true, writesGithub: false }],
        when: 'next_tick', reversal: 'Advance', gate: null,
      },
      unblock: { available: false, unavailableReason: 'Not blocked.', immediate: [], then: [], when: 'never', reversal: null, gate: 'initiative.blocked' },
      materialize_accept: { available: false, unavailableReason: 'No decomposition is waiting.', immediate: [], then: [], when: 'never', reversal: null, gate: 'materialization.accept' },
      materialize_reject: { available: false, unavailableReason: 'No decomposition is waiting.', immediate: [], then: [], when: 'never', reversal: null, gate: null },
    },
    tolerance: { effective: 'cautious', configured: 'cautious', autoresolveEnabled: true, synthesizerMode: 'on' },
    tickSeconds: 60,
  }

  it('the Track tab shows the outlook card from the guidance', () => {
    h.state.data = { ...h.initiative, guidance }
    mount()
    expect(screen.getByRole('heading', { name: 'What happens next' })).toBeInTheDocument()
    expect(screen.getByText('Your move')).toBeInTheDocument()
    expect(screen.getByText(/Risk tolerance cautious/)).toBeInTheDocument()
  })

  it('the Advance confirm lists the computed consequence instead of the static line', () => {
    h.state.data = { ...h.initiative, guidance }
    mount()
    fireEvent.click(screen.getByRole('button', { name: /Advance/ }))
    expect(screen.getByText('Moves the gate to deploy.')).toBeInTheDocument()
    expect(screen.getByText('Reverse: Step back')).toBeInTheDocument()
    expect(screen.queryByText(/This moves the human gate/)).not.toBeInTheDocument()
  })

  it('the Step back confirm says a session will start, and when', () => {
    h.state.data = { ...h.initiative, guidance }
    mount()
    fireEvent.click(screen.getByRole('button', { name: 'Step back' }))
    expect(screen.getByText('starts a session')).toBeInTheDocument()
    expect(screen.getByText('Then · in about a minute')).toBeInTheDocument()
  })

  it('a disabled control carries the server reason as its tooltip', () => {
    h.state.data = {
      ...h.initiative, status: 'blocked',
      guidance: { ...guidance, actions: { ...guidance.actions,
        advance: { ...guidance.actions.advance, available: false, unavailableReason: 'Clear the block first.' },
        unblock: { ...guidance.actions.unblock, available: true, unavailableReason: null,
          immediate: [{ text: 'Clears the block; the gate stays at build.', spawnsSession: false, writesGithub: false }] } } },
    }
    mount()
    expect(screen.getByRole('button', { name: /Advance/ })).toHaveAttribute('title', 'Clear the block first.')
    fireEvent.click(screen.getByRole('button', { name: 'Unblock' }))
    expect(screen.getByText('Clears the block; the gate stays at build.')).toBeInTheDocument()
  })

  it('without guidance the confirms fall back to their static copy and the Track tab shows no card', () => {
    h.state.data = { ...h.initiative, guidance: null }
    mount()
    expect(screen.queryByRole('heading', { name: 'What happens next' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Advance/ }))
    expect(screen.getByText(/This moves the human gate/)).toBeInTheDocument()
  })
})
