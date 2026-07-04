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

describe('SessionCard autopilot active indicator', () => {
  it('shows the live robot marker when autopilot assisted the session', () => {
    renderCard({ ...base, assistedBy: 'autopilot' })
    expect(screen.getByTestId('card-autopilot-active')).toBeInTheDocument()
  })

  it('adds an emerald edge accent to the card when guided by autopilot', () => {
    const { container } = renderCard({ ...base, assistedBy: 'autopilot' })
    expect(container.querySelector('article')?.className).toMatch(/border-l-emerald/)
  })

  it('vetoes the marker and edge accent when autopilot is paused', () => {
    const { container } = renderCard({ ...base, assistedBy: 'autopilot', autopilotPaused: true })
    expect(screen.queryByTestId('card-autopilot-active')).not.toBeInTheDocument()
    expect(container.querySelector('article')?.className).not.toMatch(/border-l-emerald/)
  })

  it('shows no marker when the session was not assisted by autopilot', () => {
    renderCard(base)
    expect(screen.queryByTestId('card-autopilot-active')).not.toBeInTheDocument()
  })

  it('does not treat other automations as autopilot', () => {
    renderCard({ ...base, assistedBy: 'pr-doctor' })
    expect(screen.queryByTestId('card-autopilot-active')).not.toBeInTheDocument()
  })

  it('pulses the marker only while the session is actively working', () => {
    renderCard({ ...base, assistedBy: 'autopilot', status: 'in_progress' })
    expect(screen.getByTestId('card-autopilot-active').className).toMatch(/animate-pulse/)
  })

  it('does not pulse when guided but idle', () => {
    renderCard({ ...base, assistedBy: 'autopilot', status: 'idle' })
    expect(screen.getByTestId('card-autopilot-active').className).not.toMatch(/animate-pulse/)
  })
})
