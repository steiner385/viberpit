// web/src/features/sessions/list/SessionCard.handBack.test.tsx
//
// #2321. A handed-back session wants the user, but there is no card to tap — so the
// pill alone ("Handed back") tells them something is wanted without telling them WHAT,
// and the only way to find out is to open the session and read the transcript. The
// session already said it in one sentence when it called `hand_back(reason)`; show it.
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { SessionCard } from './SessionCard'
import { ViewportProvider } from '@/shell/useViewport'
import type { Session } from '@/api/types'
import { session as makeSession } from '@/test/factories'

function renderCard(session: Session) {
  return render(
    <ViewportProvider kiosk={null}>
      <SessionCard
        session={session}
        account={undefined}
        onOpen={() => {}}
        onRename={vi.fn()}
        onClose={vi.fn()}
        onDelete={vi.fn()}
        onQuickResume={vi.fn()}
      />
    </ViewportProvider>
  )
}

const REASON = 'Need a decision on whether to override the read-only constraint'

describe('SessionCard hand-back reason', () => {
  it('shows the reason beside the status phrase on a handed-back session', () => {
    renderCard(makeSession({ status: 'handed_back', handBackReason: REASON }))
    expect(screen.getByTestId('card-hand-back-reason')).toHaveTextContent(REASON)
  })

  it('labels the pill "Handed back", never "Answer needed"', () => {
    renderCard(makeSession({ status: 'handed_back', handBackReason: REASON }))
    expect(screen.getByLabelText('Status: Handed back')).toBeInTheDocument()
    expect(screen.queryByLabelText('Status: Answer needed')).toBeNull()
  })

  it('omits the line when the session handed back without a reason', () => {
    renderCard(makeSession({ status: 'handed_back' }))
    expect(screen.queryByTestId('card-hand-back-reason')).toBeNull()
  })

  it('shows a mid-turn ask beside "Working…", marked as asked (#2647)', () => {
    // The server writes the reason the moment a hand_back call is read and clears it
    // on the next turn's first write, so beside a busy status it is a live ask, not a
    // leftover. Prod 2026-10-02: a session asked for an approval and kept working for
    // 13 minutes; the card read "Working…" with nothing else.
    renderCard(makeSession({ status: 'in_progress', handBackReason: REASON }))
    const ask = screen.getByTestId('card-hand-back-reason')
    expect(ask).toHaveTextContent(`asked you: ${REASON}`)
    expect(ask).toHaveAttribute('data-mid-turn', 'true')
    expect(screen.getByLabelText('Status: Working…')).toBeInTheDocument()
  })

  it('never shows a stale reason beside a resting status', () => {
    // Mirrors the prResolution rule: a resumed session keeps its old row value, and
    // "needs a decision" beside "Your turn" would be a claim the session is not making.
    for (const status of ['done', 'idle', 'complete'] as const) {
      const { unmount } = renderCard(makeSession({ status, handBackReason: REASON }))
      expect(screen.queryByTestId('card-hand-back-reason')).toBeNull()
      unmount()
    }
  })

  it('does not also render the generic summary, which would compete with it', () => {
    renderCard(makeSession({
      status: 'handed_back', handBackReason: REASON, summary: 'Investigated the queue leak',
    }))
    expect(screen.getByTestId('card-hand-back-reason')).toBeInTheDocument()
    expect(screen.queryByTestId('card-summary')).toBeNull()
  })
})
