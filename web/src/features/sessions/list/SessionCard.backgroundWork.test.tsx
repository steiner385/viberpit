import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { SessionCard } from './SessionCard'
import { ViewportProvider } from '@/shell/useViewport'
import type { Session } from '@/api/types'

function makeSession(over: Partial<Session> = {}): Session {
  return { id: 's1', name: 'My session', accountName: 'acct', status: 'waiting_external',
    createdAt: new Date().toISOString(), prs: [], ...over } as Session
}
function renderCard(session: Session) {
  return render(<ViewportProvider kiosk={null}><MemoryRouter>
    <SessionCard session={session} account={undefined} onOpen={() => {}} onRename={vi.fn()} onClose={vi.fn()} onDelete={vi.fn()} onQuickResume={vi.fn()} />
  </MemoryRouter></ViewportProvider>)
}
const work = { items: [{ toolUseId: 'tu1', kind: 'task', name: 'Bash', description: 'pytest -q', startedAt: null }], wakeup: true, wakeupAt: null }

describe('SessionCard background work (#2635)', () => {
  it('shows the summary pill collapsed and the rows expanded', () => {
    renderCard(makeSession({ backgroundWork: work }))
    expect(screen.getByTestId('card-background-pill')).toHaveTextContent('1 shell · wakeup armed')
    expect(screen.queryByRole('group', { name: 'Background work' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Show background work' }))
    expect(screen.getByRole('group', { name: 'Background work' })).toHaveTextContent('pytest -q')
  })
  it('titles the Waiting… pill with what it waits on', () => {
    renderCard(makeSession({ backgroundWork: work }))
    expect(screen.getByLabelText('Status: Waiting…')).toHaveAttribute('title', 'Waiting on 1 shell · wakeup armed')
  })
  it('renders nothing extra when there is no background work', () => {
    renderCard(makeSession({ backgroundWork: { items: [], wakeup: false, wakeupAt: null } }))
    expect(screen.queryByTestId('card-background-pill')).toBeNull()
    expect(screen.getByLabelText('Status: Waiting…')).not.toHaveAttribute('title')
  })
})
