import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect, vi } from 'vitest'
import { SessionCard } from '../SessionCard'
import type { Session } from '@/api/types'

const base: Session = {
  id: '1',
  name: 'Fix login bug',
  accountName: 'work',
  status: 'done',
  createdAt: '2026-06-22T11:00:00Z',
  updatedAt: '2026-06-22T11:58:00Z',
}

const noop = () => {}

function renderCard(session: Session, onToggleAutopilot: (id: string, paused: boolean) => void = noop) {
  return render(
    <MemoryRouter>
      <SessionCard session={session} account={undefined}
        onOpen={noop} onRename={noop} onClose={noop} onDelete={noop}
        onToggleAutopilot={onToggleAutopilot} />
    </MemoryRouter>,
  )
}

describe('SessionCard autopilot toggle', () => {
  it('shows a governed (on) toggle for a non-paused session, pressed', () => {
    renderCard({ ...base, autopilotPaused: false })
    const btn = screen.getByLabelText(/autopilot on/i)
    expect(btn).toBeInTheDocument()
    expect(btn).toHaveAttribute('aria-pressed', 'true')
  })

  it('reflects the paused state in the toggle and shows a Manual badge', () => {
    renderCard({ ...base, autopilotPaused: true })
    const btn = screen.getByLabelText(/autopilot paused/i)
    expect(btn).toHaveAttribute('aria-pressed', 'false')
    expect(screen.getByTestId('card-autopilot-badge')).toHaveTextContent('Manual')
  })

  it('requests the opposite state on click', () => {
    const onToggle = vi.fn()
    renderCard({ ...base, autopilotPaused: false }, onToggle)
    fireEvent.click(screen.getByTestId('card-autopilot'))
    expect(onToggle).toHaveBeenCalledWith('1', true)
  })

  it('is absent when no toggle handler is provided', () => {
    render(
      <MemoryRouter>
        <SessionCard session={{ ...base, autopilotPaused: false }} account={undefined}
          onOpen={noop} onRename={noop} onClose={noop} onDelete={noop} />
      </MemoryRouter>,
    )
    expect(screen.queryByTestId('card-autopilot')).not.toBeInTheDocument()
  })
})
