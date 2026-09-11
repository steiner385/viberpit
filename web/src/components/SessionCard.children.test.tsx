// Lineage on the card (child-session spec §4.6): "child of" pill + child rows.
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { SessionCard } from './SessionCard'
import type { Session } from '@/api/types'

function makeSession(over: Partial<Session> = {}): Session {
  return {
    id: 's1', name: 'My session', accountName: 'acct', status: 'in_progress',
    createdAt: new Date().toISOString(), prs: [], ...over,
  } as Session
}

function renderCard(session: Session, extra: { childSessions?: Session[]; parentName?: string | null; onOpen?: (id: string) => void } = {}) {
  return render(
    <MemoryRouter>
      <SessionCard
        session={session}
        account={undefined}
        onOpen={extra.onOpen ?? (() => {})}
        onRename={vi.fn()}
        onClose={vi.fn()}
        onDelete={vi.fn()}
        onQuickResume={vi.fn()}
        childSessions={extra.childSessions}
        parentName={extra.parentName}
      />
    </MemoryRouter>
  )
}

describe('SessionCard lineage', () => {
  it('shows a "child of" pill that opens the parent', () => {
    const onOpen = vi.fn()
    renderCard(makeSession({ id: 'c1', parentSessionId: 'p1' }), { parentName: 'Parent', onOpen })
    const pill = screen.getByTestId('card-parent-pill')
    expect(pill).toHaveTextContent('child of Parent')
    fireEvent.click(pill)
    expect(onOpen).toHaveBeenCalledWith('p1')
  })

  it('falls back to the short parent id when the parent is not loaded', () => {
    renderCard(makeSession({ id: 'c1', parentSessionId: 'p1234567890' }), { parentName: null })
    expect(screen.getByTestId('card-parent-pill')).toHaveTextContent('child of p1234567')
  })

  it('shows a children count collapsed and the child rows when expanded', () => {
    const child = makeSession({ id: 'c1', name: 'API redesign', status: 'waiting_question', parentSessionId: 's1' })
    renderCard(makeSession(), { childSessions: [child] })
    expect(screen.getByText(/1 child/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Open child session/ })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /^Show child sessions$/ }))
    expect(screen.getByRole('button', { name: /Open child session API redesign/ })).toBeInTheDocument()
  })

  it('renders no pill and no count for a root without children', () => {
    renderCard(makeSession())
    expect(screen.queryByTestId('card-parent-pill')).toBeNull()
    expect(screen.queryByText(/child/)).toBeNull()
  })
})
