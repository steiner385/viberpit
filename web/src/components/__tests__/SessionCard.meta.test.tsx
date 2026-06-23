import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect } from 'vitest'
import { SessionCard } from '../SessionCard'
import type { Session, SubAgentSummary } from '@/api/types'

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
      <SessionCard
        session={session}
        account={undefined}
        onOpen={noop}
        onRename={noop}
        onClose={noop}
        onDelete={noop}
      />
    </MemoryRouter>,
  )
}

const agents = (n: number): SubAgentSummary[] =>
  Array.from({ length: n }, (_, i) => ({
    agentId: `a${i}`,
    parentAgentId: null,
    agentType: 'general',
    description: 'work',
    status: 'running' as const,
  }))

describe('SessionCard last-active', () => {
  it('shows a relative last-active label from updatedAt', () => {
    renderCard(base)
    expect(screen.getByText(/^active \d+[smhd] ago$/)).toBeInTheDocument()
  })

  it('omits the label when updatedAt is missing/invalid', () => {
    renderCard({ ...base, updatedAt: '' })
    expect(screen.queryByText(/active/)).not.toBeInTheDocument()
  })

  it('omits the label when updatedAt is null (never shows the 1970 epoch)', () => {
    // The backend can send null; `new Date(null)` is epoch 0 (a *finite* time),
    // so a naive isFinite guard would render "active 20627d ago". Reject it.
    renderCard({ ...base, updatedAt: null as unknown as string })
    expect(screen.queryByText(/active/)).not.toBeInTheDocument()
  })
})

describe('SessionCard collapsed sub-agent count', () => {
  it('shows the count pill when collapsed', () => {
    renderCard({ ...base, subAgents: agents(3) })
    expect(screen.getByText('3 agents')).toBeInTheDocument()
  })

  it('singularizes a single sub-agent', () => {
    renderCard({ ...base, subAgents: agents(1) })
    expect(screen.getByText('1 agent')).toBeInTheDocument()
  })

  it('hides the count pill once expanded', () => {
    renderCard({ ...base, subAgents: agents(2) })
    expect(screen.getByText('2 agents')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Show sub-agents' }))
    expect(screen.queryByText('2 agents')).not.toBeInTheDocument()
  })

  it('shows no pill when there are no sub-agents', () => {
    renderCard(base)
    expect(screen.queryByText(/agent/)).not.toBeInTheDocument()
  })
})
