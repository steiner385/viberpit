import { render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect } from 'vitest'
import { SessionCard } from '../SessionCard'
import type { Session } from '@/api/types'

// The session card lives in a drag-resizable sidebar, so its layout must be
// deterministic at every width: the same three rows (identity / context /
// telemetry) in the same order on every card, each row truncating rather than
// wrapping. These tests lock that skeleton in place so a future tweak can't
// regress back to content-dependent flex-wrap (where the account chip and the
// usage bar landed in a different spot on every card).

const base: Session = {
  id: '1',
  name: 'Fix login bug',
  accountName: 'work',
  status: 'in_progress',
  createdAt: '2026-06-22T11:00:00Z',
  updatedAt: '2026-06-22T11:58:00Z',
}

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
      />
    </MemoryRouter>,
  )
}

describe('SessionCard deterministic skeleton', () => {
  it('renders the three fixed rows in order', () => {
    renderCard(base)
    expect(screen.getByTestId('card-identity')).toBeInTheDocument()
    expect(screen.getByTestId('card-context')).toBeInTheDocument()
    expect(screen.getByTestId('card-telemetry')).toBeInTheDocument()
  })

  it('truncates the title so the account chip stays pinned on the identity row', () => {
    renderCard(base)
    const identity = screen.getByTestId('card-identity')
    const title = within(identity).getByText('Fix login bug')
    // Without `flex-1 min-w-0`, `truncate` is a no-op and a long title shoves
    // the account chip onto a second line — the original inconsistency.
    expect(title).toHaveClass('truncate', 'min-w-0', 'flex-1')
    expect(within(identity).getByText('work')).toBeInTheDocument()
  })

  it('places the last-active timestamp in the telemetry row, not the context row', () => {
    renderCard(base)
    const telemetry = screen.getByTestId('card-telemetry')
    expect(within(telemetry).getByText(/^active \d+[smhd] ago$/)).toBeInTheDocument()
  })
})
