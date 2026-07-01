import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect } from 'vitest'
import { SessionCard } from '../SessionCard'
import type { Session } from '@/api/types'

const base: Session = {
  id: '1',
  name: 'Fix login bug',
  accountName: 'work',
  status: 'idle',
  createdAt: '2026-06-22T11:00:00Z',
  updatedAt: '2026-06-22T11:58:00Z',
  prs: [],
}

const noop = () => {}

function renderCard(session: Session) {
  return render(
    <MemoryRouter>
      <SessionCard
        session={session}
        account={undefined}
        onOpen={noop}
        onRename={noop}
        onClose={noop}
        onDelete={noop}
        onQuickResume={noop}
      />
    </MemoryRouter>,
  )
}

describe('SessionCard provenance badge', () => {
  it('shows "Created by PR Doctor" badge when origin is pr-doctor', () => {
    renderCard({ ...base, origin: 'pr-doctor' })
    const badge = screen.getByTestId('card-provenance')
    expect(badge).toBeInTheDocument()
    expect(badge).toHaveTextContent('Created by PR Doctor')
  })

  it('links to /automation', () => {
    renderCard({ ...base, origin: 'pr-doctor' })
    const badge = screen.getByTestId('card-provenance')
    expect(badge).toHaveAttribute('href', '/automation')
  })

  it('shows raw origin name for unknown automations', () => {
    renderCard({ ...base, origin: 'custom-bot' })
    const badge = screen.getByTestId('card-provenance')
    expect(badge).toHaveTextContent('Created by custom-bot')
  })

  it('does not render the badge when origin is absent', () => {
    renderCard(base)
    expect(screen.queryByTestId('card-provenance')).not.toBeInTheDocument()
  })

  it('does not render the badge when origin is null', () => {
    renderCard({ ...base, origin: null })
    expect(screen.queryByTestId('card-provenance')).not.toBeInTheDocument()
  })
})
