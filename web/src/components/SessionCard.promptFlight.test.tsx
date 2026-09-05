/**
 * The card must stop reading "Ready" the moment its session has a prompt in flight —
 * the failure the user actually reported, seen from the list rather than from an open
 * transcript.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { Session } from '@/api/types'
import { noteSent, noteStage, __resetFlight } from '@/lib/promptFlight'
import { SessionCard } from './SessionCard'

vi.mock('@/api/hooks', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/hooks')>()
  return { ...actual, useAccounts: () => ({ data: [] }), useSettings: () => ({ data: {} }) }
})

const session = (over: Partial<Session> = {}): Session => ({
  id: 's1', name: 'my session', status: 'idle', account: 'alpha', liveness: 'fresh',
  ...over,
} as Session)

function renderCard(s: Session) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <SessionCard
          session={s}
          account={undefined}
          onOpen={vi.fn()}
          onRename={vi.fn()}
          onClose={vi.fn()}
          onDelete={vi.fn()}
          onQuickResume={vi.fn()}
        />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('SessionCard — prompt in flight', () => {
  beforeEach(() => { __resetFlight() })

  it('shows "Ready" with nothing in flight', () => {
    renderCard(session())
    expect(screen.getByText('Ready')).toBeInTheDocument()
  })

  it('shows the in-flight pill once a prompt has been sent to this session', () => {
    noteSent('s1', 'sent')
    renderCard(session())
    expect(screen.queryByText('Ready')).not.toBeInTheDocument()
    expect(screen.getByText('Sending…')).toBeInTheDocument()
  })

  it('follows the stage as the edge acks it', () => {
    noteSent('s1', 'sent')
    noteStage('s1', 'resuming')
    renderCard(session())
    expect(screen.getByText('Sent — waking…')).toBeInTheDocument()
  })

  it('is unaffected by a prompt in flight to a DIFFERENT session', () => {
    noteSent('other', 'sent')
    renderCard(session())
    expect(screen.getByText('Ready')).toBeInTheDocument()
  })
})
