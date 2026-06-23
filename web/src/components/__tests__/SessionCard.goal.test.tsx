import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect } from 'vitest'
import { SessionCard } from '../SessionCard'
import type { Session } from '@/api/types'

const base: Session = {
  id: '1',
  name: 'Fix login bug',
  accountName: 'work',
  status: 'in_progress',
  createdAt: '2026-06-22T11:00:00Z',
  updatedAt: '2026-06-22T11:58:00Z',
}

const noop = () => {}

function renderCard(session: Session) {
  return render(
    <MemoryRouter>
      <SessionCard session={session} account={undefined}
        onOpen={noop} onRename={noop} onClose={noop} onDelete={noop} />
    </MemoryRouter>,
  )
}

describe('SessionCard goal badge', () => {
  it('shows a Goal pill with the goal text as tooltip when activeGoal is set', () => {
    renderCard({ ...base, activeGoal: 'ship the PR' })
    const pill = screen.getByText('Goal')
    expect(pill).toBeInTheDocument()
    expect(pill).toHaveAttribute('title', 'Goal: ship the PR')
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
