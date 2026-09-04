import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect } from 'vitest'
import { SessionCard } from '../SessionCard'
import type { Session } from '@/api/types'
import { session } from '@/test/factories'

const base: Session = session({
  id: '1',
  name: 'Fix login bug',
  accountName: 'work',
  status: 'in_progress',
  createdAt: '2026-06-22T11:00:00Z',
  updatedAt: '2026-06-22T11:58:00Z',
})

const noop = () => {}

function renderCard(session: Session) {
  return render(
    <MemoryRouter>
      <SessionCard session={session} account={undefined}
        onOpen={noop} onRename={noop} onClose={noop} onDelete={noop} onQuickResume={noop} />
    </MemoryRouter>,
  )
}

describe('SessionCard goal badge', () => {
  it('shows a Goal pill with the goal text as tooltip when activeGoal is set', () => {
    renderCard({ ...base, activeGoal: 'ship the PR' })
    const pill = screen.getByTitle('Goal: ship the PR')
    expect(pill).toBeInTheDocument()
    expect(pill).toHaveTextContent('Goal')
  })

  it('hides the Goal pill when activeGoal is null', () => {
    renderCard({ ...base, activeGoal: null })
    expect(screen.queryByText('Goal')).not.toBeInTheDocument()
  })

  it('hides the Goal pill when activeGoal is absent', () => {
    renderCard(base)
    expect(screen.queryByText('Goal')).not.toBeInTheDocument()
  })
})

describe('SessionCard inferred goal badge', () => {
  it('shows a subdued Goal pill with tooltip when the goal is inferred', () => {
    renderCard({ ...base, effectiveGoal: 'Ship the referral schema PR', goalSource: 'inferred' })
    const pill = screen.getByTitle('Goal (inferred): Ship the referral schema PR')
    expect(pill).toBeInTheDocument()
    expect(pill).toHaveTextContent('Goal')
  })

  it('explicit /goal pill takes precedence over the inferred variant', () => {
    renderCard({ ...base, activeGoal: 'ship it', effectiveGoal: 'ship it', goalSource: 'explicit' })
    expect(screen.getByTitle('Goal: ship it')).toBeInTheDocument()
    expect(screen.queryByTitle(/inferred/)).not.toBeInTheDocument()
  })

  it('no pill when the goal is just the title fallback', () => {
    renderCard({ ...base, effectiveGoal: base.name, goalSource: 'initial_prompt' })
    expect(screen.queryByText('Goal')).not.toBeInTheDocument()
  })
})
