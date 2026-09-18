import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect, vi } from 'vitest'
import { SessionCard } from './SessionCard'
import { ViewportProvider } from '@/shell/useViewport'
import type { Session } from '@/api/types'
import { session } from '@/test/factories'

const base: Session = session({
  id: '1',
  name: 'Fix login bug',
  accountName: 'work',
  status: 'done',
  createdAt: '2026-06-22T11:00:00Z',
  updatedAt: '2026-06-22T11:58:00Z',
})

const noop = () => {}

function renderCard(session: Session, onToggleAutoArchive: (id: string, optedOut: boolean) => void = noop) {
  return render(
    <ViewportProvider kiosk={null}>
      <MemoryRouter>
        <SessionCard session={session} account={undefined}
          onOpen={noop} onRename={noop} onClose={noop} onDelete={noop} onQuickResume={noop}
          onToggleAutoArchive={onToggleAutoArchive} />
      </MemoryRouter>
    </ViewportProvider>,
  )
}

function openMenu() {
  fireEvent.click(screen.getByRole('button', { name: /more actions for session/i }))
}

describe('SessionCard auto-archive toggle', () => {
  it('offers "Disable auto-archive" in the ⋯ menu for an eligible session', () => {
    renderCard({ ...base, autoArchiveOptOut: null })
    openMenu()
    expect(screen.getByTestId('card-overflow-auto-archive')).toHaveTextContent('Disable auto-archive')
  })

  it('offers "Enable auto-archive" once opted out mid-session, and labels the badge accordingly', () => {
    renderCard({ ...base, autoArchiveOptOut: 'session-toggle' })
    openMenu()
    expect(screen.getByTestId('card-overflow-auto-archive')).toHaveTextContent('Enable auto-archive')
    expect(screen.getByTestId('card-no-autoarchive-badge')).toHaveAttribute(
      'title', expect.stringMatching(/turned off for this session/i))
  })

  it('requests the opposite state on click', () => {
    const onToggle = vi.fn()
    renderCard({ ...base, autoArchiveOptOut: null }, onToggle)
    openMenu()
    fireEvent.click(screen.getByTestId('card-overflow-auto-archive'))
    expect(onToggle).toHaveBeenCalledWith('1', true)
  })

  it('requests re-enrollment when clicked from an opted-out state', () => {
    const onToggle = vi.fn()
    renderCard({ ...base, autoArchiveOptOut: 'create-opt-out' }, onToggle)
    openMenu()
    fireEvent.click(screen.getByTestId('card-overflow-auto-archive'))
    expect(onToggle).toHaveBeenCalledWith('1', false)
  })

  it('is absent when no toggle handler is provided', () => {
    render(
      <ViewportProvider kiosk={null}>
        <MemoryRouter>
          <SessionCard session={{ ...base, autoArchiveOptOut: null }} account={undefined}
            onOpen={noop} onRename={noop} onClose={noop} onDelete={noop} onQuickResume={noop} />
        </MemoryRouter>
      </ViewportProvider>,
    )
    openMenu()
    expect(screen.queryByTestId('card-overflow-auto-archive')).not.toBeInTheDocument()
  })
})
