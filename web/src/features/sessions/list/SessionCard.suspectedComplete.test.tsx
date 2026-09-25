// web/src/features/sessions/list/SessionCard.suspectedComplete.test.tsx
//
// The suspected-complete overlay: a handed-back session whose hand-back was a status
// report on a met objective (prod 2026-09-25, "Status only, no action needed" over an
// armed PR). The server writes its verdict beside the hand-back reason; the card shows
// it as a hint with two one-tap answers and never archives on its own.
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { SessionCard } from './SessionCard'
import { ViewportProvider } from '@/shell/useViewport'
import type { Session } from '@/api/types'
import { session as makeSession } from '@/test/factories'

function renderCard(session: Session, onResolve?: (id: string, decision: 'confirm' | 'reject') => void) {
  const onOpen = vi.fn()
  render(
    <ViewportProvider kiosk={null}>
      <SessionCard
        session={session}
        account={undefined}
        onOpen={onOpen}
        onRename={vi.fn()}
        onClose={vi.fn()}
        onDelete={vi.fn()}
        onQuickResume={vi.fn()}
        onResolveSuspectedComplete={onResolve}
      />
    </ViewportProvider>
  )
  return { onOpen }
}

const REASON = 'Status only, no action needed: PR #2469 is open, ready, auto-merge armed'
const BAR = 'PR #2469 open with auto-merge armed'

describe('SessionCard suspected-complete overlay', () => {
  it('shows the "Likely complete" hint with the bar reason beside a handed-back session', () => {
    renderCard(makeSession({ status: 'handed_back', handBackReason: REASON, suspectedCompleteReason: BAR }))
    const hint = screen.getByTestId('card-suspected-complete')
    expect(hint).toHaveTextContent('Likely complete')
    expect(screen.getByTitle(`Likely complete: ${BAR}`)).toBeInTheDocument()
    // The pill stays "Handed back": the overlay is a hint on it, not a new status.
    expect(screen.getByLabelText('Status: Handed back')).toBeInTheDocument()
    expect(screen.getByTestId('card-hand-back-reason')).toHaveTextContent(REASON)
  })

  it('routes the two answers to the parent with the session id, without opening the card', () => {
    const onResolve = vi.fn()
    const { onOpen } = renderCard(
      makeSession({ id: 's-9', status: 'handed_back', handBackReason: REASON, suspectedCompleteReason: BAR }),
      onResolve,
    )
    fireEvent.click(screen.getByTestId('card-suspected-complete-confirm'))
    expect(onResolve).toHaveBeenLastCalledWith('s-9', 'confirm')
    fireEvent.click(screen.getByTestId('card-suspected-complete-reject'))
    expect(onResolve).toHaveBeenLastCalledWith('s-9', 'reject')
    expect(onOpen).not.toHaveBeenCalled()
  })

  it('renders the hint but no buttons when the parent owns no mutation', () => {
    renderCard(makeSession({ status: 'handed_back', suspectedCompleteReason: BAR }))
    expect(screen.getByTestId('card-suspected-complete')).toBeInTheDocument()
    expect(screen.queryByTestId('card-suspected-complete-confirm')).toBeNull()
    expect(screen.queryByTestId('card-suspected-complete-reject')).toBeNull()
  })

  it('omits the overlay on a plain hand-back', () => {
    renderCard(makeSession({ status: 'handed_back', handBackReason: 'Need your call on the scope' }))
    expect(screen.queryByTestId('card-suspected-complete')).toBeNull()
  })

  it('never shows a stale verdict once the session is running again', () => {
    // Same gate as handBackReason: the row keeps its value until the next status
    // write clears it, and "Likely complete" beside "Working…" is a claim the
    // server is not making.
    renderCard(makeSession({ status: 'in_progress', suspectedCompleteReason: BAR }))
    expect(screen.queryByTestId('card-suspected-complete')).toBeNull()
  })
})
