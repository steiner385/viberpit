// web/src/features/roadmap/guidance/ConsequenceList.test.tsx
// THE renderer for a consequence: now-steps, then-steps with the tick-derived
// timing, the two cost badges, and the reverse line. Nothing else formats one.
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import type { Consequence, InitiativeGuidance } from '@/api/sdlc'
import { ConsequenceList, whenLabel } from './ConsequenceList'
import { WhatHappensNext } from './WhatHappensNext'

const advance: Consequence = {
  available: true, unavailableReason: null,
  immediate: [{ text: 'Moves the gate to plan.', spawnsSession: false, writesGithub: false }],
  then: [{ text: 'Spawns a plan session on opus.', spawnsSession: true, writesGithub: false }],
  when: 'next_tick', reversal: 'Step back', gate: 'stage.advance',
}

describe('whenLabel', () => {
  it('derives the timing from the tick, never a literal', () => {
    expect(whenLabel('next_tick', 60)).toBe('in about a minute')
    expect(whenLabel('next_tick', 300)).toBe('in about 5 minutes')
    expect(whenLabel('next_tick')).toBe('on the next automation run')
    expect(whenLabel('now', 60)).toBeNull()
    expect(whenLabel('never', 60)).toBeNull()
  })
})

describe('ConsequenceList', () => {
  it('renders now and then with the cost badge and the reverse', () => {
    render(<ConsequenceList consequence={advance} tickSeconds={60} />)
    expect(screen.getByText('Now')).toBeInTheDocument()
    expect(screen.getByText('Moves the gate to plan.')).toBeInTheDocument()
    expect(screen.getByText('Then · in about a minute')).toBeInTheDocument()
    expect(screen.getByText('Spawns a plan session on opus.')).toBeInTheDocument()
    expect(screen.getByText('starts a session')).toBeInTheDocument()
    expect(screen.queryByText('writes to GitHub')).not.toBeInTheDocument()
    expect(screen.getByText('Reverse: Step back')).toBeInTheDocument()
  })

  it('shows the GitHub badge and an optional heading', () => {
    const accept: Consequence = { ...advance, when: 'now', reversal: null,
      immediate: [{ text: 'Creates the epic.', spawnsSession: false, writesGithub: true }], then: [] }
    render(<ConsequenceList consequence={accept} heading="Accept" />)
    expect(screen.getByText('Accept')).toBeInTheDocument()
    expect(screen.getByText('writes to GitHub')).toBeInTheDocument()
    expect(screen.queryByText(/^Then/)).not.toBeInTheDocument()
    expect(screen.queryByText(/^Reverse/)).not.toBeInTheDocument()
  })

  it('says why an unavailable control is unavailable', () => {
    render(<ConsequenceList consequence={{ ...advance, available: false, unavailableReason: 'Clear the block first.' }} />)
    expect(screen.getByText('Clear the block first.')).toBeInTheDocument()
  })
})

describe('WhatHappensNext', () => {
  const guidance: InitiativeGuidance = {
    machine: {
      actor: 'machine', when: 'next_tick', summary: 'Advances to plan automatically.',
      steps: [
        { text: 'Advances to plan automatically.', spawnsSession: false, writesGithub: false },
        { text: 'Spawns a plan session on opus.', spawnsSession: true, writesGithub: false },
      ],
      because: 'Risk tolerance balanced: the machine resolves these gates itself: advance a stage once its artifact merged.',
    },
    actions: { advance, step_back: advance, unblock: advance, materialize_accept: advance, materialize_reject: advance },
    tolerance: { effective: 'balanced', configured: 'balanced', autoresolveEnabled: true, synthesizerMode: 'on' },
    tickSeconds: 60,
  }

  it('names the actor, the timing, the steps and the reason', () => {
    render(<WhatHappensNext guidance={guidance} />)
    expect(screen.getByRole('heading', { name: 'What happens next' })).toBeInTheDocument()
    expect(screen.getByText('Automatic')).toBeInTheDocument()
    expect(screen.getByText('in about a minute')).toBeInTheDocument()
    expect(screen.getByText('Spawns a plan session on opus.')).toBeInTheDocument()
    expect(screen.getByText(/Risk tolerance balanced/)).toBeInTheDocument()
  })

  it('labels your move and nothing-to-do, and renders nothing for a program', () => {
    const you = { ...guidance, machine: { ...guidance.machine!, actor: 'you', when: 'never' } }
    const { unmount } = render(<WhatHappensNext guidance={you} />)
    expect(screen.getByText('Your move')).toBeInTheDocument()
    expect(screen.queryByText(/about a minute/)).not.toBeInTheDocument()
    unmount()
    render(<WhatHappensNext guidance={{ ...guidance, machine: { ...guidance.machine!, actor: 'nobody' } }} />)
    expect(screen.getByText('Nothing to do')).toBeInTheDocument()
    const { container } = render(<WhatHappensNext guidance={{ ...guidance, machine: null }} />)
    expect(container).toBeEmptyDOMElement()
  })
})
