import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { SessionCard } from './SessionCard'
import { ViewportProvider } from '@/shell/useViewport'
import type { Session } from '@/api/types'

function makeSession(over: Partial<Session> = {}): Session {
  return {
    id: 's1', name: 'My session', accountName: 'acct', status: 'in_progress',
    createdAt: new Date().toISOString(), prs: [], ...over,
  } as Session
}
function renderCard(session: Session) {
  return render(
    <MemoryRouter>
      <ViewportProvider kiosk={null}>
        <SessionCard session={session} account={undefined}
          onOpen={() => {}} onRename={vi.fn()} onClose={vi.fn()} onDelete={vi.fn()} onQuickResume={vi.fn()} />
      </ViewportProvider>
    </MemoryRouter>,
  )
}

describe('SessionCard capacity link (spec 2026-09-20 C6)', () => {
  it('a usage-stalled session links to Delivery → Capacity', () => {
    renderCard(makeSession({ usageStalled: true }))
    expect(screen.getByRole('link', { name: 'See capacity' })).toHaveAttribute('href', '/delivery?scope=capacity')
  })
  it('a working session shows no capacity link', () => {
    renderCard(makeSession({ usageStalled: false }))
    expect(screen.queryByRole('link', { name: 'See capacity' })).toBeNull()
  })
})
