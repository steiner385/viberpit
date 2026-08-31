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

function openMenu() {
  fireEvent.click(screen.getByRole('button', { name: /more actions for session/i }))
}

describe('SessionCard autopilot toggle', () => {
  it('offers "Pause autopilot" in the ⋯ menu for a non-paused session', () => {
    renderCard({ ...base, autopilotPaused: false })
    openMenu()
    expect(screen.getByTestId('card-overflow-autopilot')).toHaveTextContent('Pause autopilot')
  })

  it('reflects the paused state in the menu item and shows a Manual marker', () => {
    renderCard({ ...base, autopilotPaused: true })
    openMenu()
    expect(screen.getByTestId('card-overflow-autopilot')).toHaveTextContent('Resume autopilot')
    expect(screen.getByTestId('card-autopilot-badge')).toHaveTextContent('Manual')
  })

  it('shows a passive "Auto-archive off" badge for a manually-unarchived session', () => {
    renderCard({ ...base, autoArchiveOptOut: 'manual-unarchive' })
    const badge = screen.getByTestId('card-no-autoarchive-badge')
    expect(badge).toHaveTextContent('Auto-archive off')
    // Passive indicator — not a button/link (a11y: no nested interactive in the row).
    expect(badge.tagName).toBe('SPAN')
  })

  it('omits the auto-archive badge when the session is still eligible', () => {
    renderCard({ ...base, autoArchiveOptOut: null })
    expect(screen.queryByTestId('card-no-autoarchive-badge')).toBeNull()
  })

  it('requests the opposite state on click', () => {
    const onToggle = vi.fn()
    renderCard({ ...base, autopilotPaused: false }, onToggle)
    openMenu()
    fireEvent.click(screen.getByTestId('card-overflow-autopilot'))
    expect(onToggle).toHaveBeenCalledWith('1', true)
  })

  it('is absent when no toggle handler is provided', () => {
    render(
      <MemoryRouter>
        <SessionCard session={{ ...base, autopilotPaused: false }} account={undefined}
          onOpen={noop} onRename={noop} onClose={noop} onDelete={noop} />
      </MemoryRouter>,
    )
    openMenu()
    expect(screen.queryByTestId('card-overflow-autopilot')).not.toBeInTheDocument()
  })
})

describe('SessionCard autopilot active indicator', () => {
  it('shows the live robot marker when autopilot assisted the session', () => {
    renderCard({ ...base, assistedBy: 'autopilot' })
    expect(screen.getByTestId('card-autopilot-active')).toBeInTheDocument()
  })

  it('does not repurpose the status rail for autopilot (the rail belongs to the status tier)', () => {
    // The old card used an emerald border-l for "guided by autopilot"; the rail
    // now encodes status tier, so the ONLY autopilot signal is the Bot marker.
    renderCard({ ...base, assistedBy: 'autopilot' }) // status 'done' → calm tier
    expect(screen.getByTestId('card-status-rail').className).not.toMatch(/emerald/)
  })

  it('vetoes the marker when autopilot is paused', () => {
    renderCard({ ...base, assistedBy: 'autopilot', autopilotPaused: true })
    expect(screen.queryByTestId('card-autopilot-active')).not.toBeInTheDocument()
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
