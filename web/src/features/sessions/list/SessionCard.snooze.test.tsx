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
  status: 'idle',
  createdAt: '2026-06-22T11:00:00Z',
  updatedAt: '2026-06-22T11:58:00Z',
})

const noop = () => {}

function renderCard(s: Session, onSnooze?: (id: string, until: string | null) => void) {
  return render(
    <ViewportProvider kiosk={null}>
      <MemoryRouter>
        <SessionCard session={s} account={undefined}
          onOpen={noop} onRename={noop} onClose={noop} onDelete={noop} onQuickResume={noop}
          onSnooze={onSnooze} />
      </MemoryRouter>
    </ViewportProvider>,
  )
}

function openMenu() {
  fireEvent.click(screen.getByRole('button', { name: /more actions for session/i }))
}

describe('SessionCard snooze menu', () => {
  it('offers the four presets in the ⋯ menu', () => {
    renderCard(base, vi.fn())
    openMenu()
    for (const id of ['1h', 'tomorrow', 'monday', 'manual']) {
      expect(screen.getByTestId(`card-overflow-snooze-${id}`)).toBeInTheDocument()
    }
  })

  it('sends an ISO time for a timed preset and null for "until I wake it"', () => {
    const onSnooze = vi.fn()
    renderCard(base, onSnooze)
    openMenu()
    fireEvent.click(screen.getByTestId('card-overflow-snooze-1h'))
    expect(onSnooze).toHaveBeenCalledWith('1', expect.stringMatching(/Z$/))
    openMenu()
    fireEvent.click(screen.getByTestId('card-overflow-snooze-manual'))
    expect(onSnooze).toHaveBeenLastCalledWith('1', null)
  })

  it('hides the presets for a pre-spawn session', () => {
    renderCard({ ...base, status: 'queued' }, vi.fn())
    openMenu()
    expect(screen.queryByTestId('card-overflow-snooze-1h')).not.toBeInTheDocument()
  })

  it('hides the presets when the parent wires no handler', () => {
    renderCard(base)
    openMenu()
    expect(screen.queryByTestId('card-overflow-snooze-1h')).not.toBeInTheDocument()
  })
})
