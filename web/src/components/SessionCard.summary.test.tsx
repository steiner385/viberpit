// web/src/components/SessionCard.summary.test.tsx
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
    <SessionCard
      session={session}
      account={undefined}
      onOpen={() => {}}
      onRename={vi.fn()}
      onClose={vi.fn()}
      onDelete={vi.fn()}
      onQuickResume={vi.fn()}
    />
  )
}

describe('SessionCard summary line', () => {
  it('renders the summary when present', () => {
    renderCard(makeSession({ summary: 'Refactoring the auth middleware' }))
    expect(screen.getByTestId('card-summary')).toHaveTextContent('Refactoring the auth middleware')
  })

  it('omits the summary line when empty', () => {
    renderCard(makeSession({ summary: '' }))
    expect(screen.queryByTestId('card-summary')).toBeNull()
  })
})
