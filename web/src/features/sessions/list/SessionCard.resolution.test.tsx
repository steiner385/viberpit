// web/src/features/sessions/list/SessionCard.resolution.test.tsx
//
// A session the server ended WITHOUT running (a queued pr-doctor session whose
// PR merged before spawn) carries the reason in `prResolution`. The card must
// say it next to the status phrase: a bare "Ended" over "No messages yet" reads
// as a session that ran and died, not one that was never needed.
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

describe('SessionCard PR resolution note', () => {
  it('shows the resolution beside the status phrase on an ended session', () => {
    renderCard(makeSession({
      status: 'ended',
      prResolution: 'obsolete: PR #15776 merged before start',
    }))
    expect(screen.getByLabelText('Status: Ended')).toBeInTheDocument()
    expect(screen.getByTestId('card-resolution')).toHaveTextContent('obsolete: PR #15776 merged before start')
  })

  it('shows the resolution on a complete session too', () => {
    renderCard(makeSession({ status: 'complete', prResolution: 'not-needed' }))
    expect(screen.getByTestId('card-resolution')).toHaveTextContent('not-needed')
  })

  it('omits the note when the session has no resolution', () => {
    renderCard(makeSession({ status: 'ended' }))
    expect(screen.queryByTestId('card-resolution')).toBeNull()
  })

  it('never shows a stale resolution on a session that is still running', () => {
    // A resumed session keeps its old row value; the note only makes sense once
    // the session has actually stopped.
    renderCard(makeSession({ status: 'in_progress', prResolution: 'not-needed' }))
    expect(screen.queryByTestId('card-resolution')).toBeNull()
  })
})

describe('SessionCard typed spawn failure (#947)', () => {
  const failure = (reason: string, message: string) => ({
    reason, message, transient: reason === 'network', attempt: 2, requeued: false,
  })

  it('says why a queued session never started', () => {
    renderCard(makeSession({
      status: 'ended',
      lastSpawnFailure: failure('network', 'Could not resolve host: github.com'),
    }))
    const chip = screen.getByTestId('card-spawn-failure')
    expect(chip).toHaveTextContent('Failed to start: Could not resolve host: github.com')
    expect(chip).toHaveAttribute('title', 'network: Could not resolve host: github.com')
  })

  it('leaves an obsolete start to the resolution note (said once)', () => {
    renderCard(makeSession({
      status: 'ended',
      prResolution: 'obsolete: PR #12 merged before start',
      lastSpawnFailure: failure('obsolete', 'PR #12 merged before start'),
    }))
    expect(screen.queryByTestId('card-spawn-failure')).toBeNull()
    expect(screen.getByTestId('card-resolution')).toBeInTheDocument()
  })

  it('never shows it on a session that is running', () => {
    renderCard(makeSession({
      status: 'in_progress',
      lastSpawnFailure: failure('start_error', 'boom'),
    }))
    expect(screen.queryByTestId('card-spawn-failure')).toBeNull()
  })
})
