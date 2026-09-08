import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect } from 'vitest'
import { SessionCard } from '../SessionCard'
import type { Session, SubAgentSummary } from '@/api/types'
import { session } from '@/test/factories'

const base: Session = session({
  id: '1',
  name: 'Fix login bug',
  accountName: 'work',
  // A handed-back (non-busy) status: `last_turn_at` only advances when a turn
  // returns, so the "active … ago" label is only meaningful — and only shown —
  // off the busy tier. Busy-tier suppression is covered explicitly below.
  status: 'done',
  createdAt: '2026-06-22T11:00:00Z',
  updatedAt: '2026-06-22T11:58:00Z',
})

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

  it('suppresses the label while the session is busy (the spinner conveys liveness)', () => {
    // `last_turn_at` freezes for the whole in-progress turn, so a session that
    // has been streaming for minutes still reads "active 10m ago" — looking
    // stale/idle next to its live "Working…" spinner. Hide the misleading clock
    // while busy; the pill already signals the session is alive.
    renderCard({ ...base, status: 'in_progress' })
    expect(screen.queryByText(/active/)).not.toBeInTheDocument()
  })

  it('suppresses the label for every busy-tier status (condensing/retrying), not just in_progress', () => {
    for (const status of ['condensing', 'retrying'] as const) {
      const { unmount } = renderCard({ ...base, status })
      expect(screen.queryByText(/active/)).not.toBeInTheDocument()
      unmount()
    }
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

describe('SessionCard desktop badge', () => {
  it('shows a desktop badge for a native-display session', () => {
    renderCard({ ...base, displayMode: 'native', host: 'dobby' })
    expect(screen.getByTitle(/windows open on the host's desktop/i)).toBeInTheDocument()
  })
  it('shows no badge for virtual or legacy sessions', () => {
    renderCard({ ...base, displayMode: 'virtual' })
    expect(screen.queryByTitle(/windows open on the host's desktop/i)).toBeNull()
  })
})
