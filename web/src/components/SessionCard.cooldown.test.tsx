import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { SessionCard } from './SessionCard'
import type { Session } from '@/api/types'

function makeSession(over: Partial<Session> = {}): Session {
  return {
    id: 's1', name: 'My session', accountName: 'acct', status: 'in_progress',
    createdAt: new Date().toISOString(), prs: [], ...over,
  } as Session
}
function renderCard(session: Session) {
  return render(
    <SessionCard session={session} account={undefined}
      onOpen={() => {}} onRename={vi.fn()} onClose={vi.fn()} onDelete={vi.fn()} onQuickResume={vi.fn()} />
  )
}

describe('SessionCard cooldown badge', () => {
  it('shows the cooldown badge while autopilotCooldownUntil is in the future', () => {
    const future = new Date(Date.now() + 90_000).toISOString()
    renderCard(makeSession({ autopilotCooldownUntil: future }))
    expect(screen.getByText(/you're steering/i)).toBeInTheDocument()
  })

  it('hides the cooldown badge when autopilotCooldownUntil is absent', () => {
    renderCard(makeSession({ autopilotCooldownUntil: null }))
    expect(screen.queryByText(/you're steering/i)).toBeNull()
  })
})
