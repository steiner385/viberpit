import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect } from 'vitest'
import { SessionCard } from './SessionCard'
import { ViewportProvider } from '@/shell/useViewport'
import type { Session } from '@/api/types'

const base: Session = {
  id: '1',
  name: 'Fix login bug',
  accountName: 'work',
  engine: 'claude_code',
  engineSessionId: null,
  status: 'idle',
  createdAt: '2026-06-22T11:00:00Z',
  updatedAt: '2026-06-22T11:58:00Z',
  prs: [],
}

const noop = () => {}

function renderCard(session: Session) {
  return render(
    <ViewportProvider kiosk={null}>
      <MemoryRouter>
        <SessionCard
          session={session}
          account={undefined}
          onOpen={noop}
          onRename={noop}
          onClose={noop}
          onDelete={noop}
          onQuickResume={noop}
        />
      </MemoryRouter>
    </ViewportProvider>,
  )
}

describe('SessionCard provenance badge', () => {
  it('shows "Created by PR Doctor" from the server-supplied originLabel', () => {
    // The label is the automation registry's display name, published on the
    // session; the card keeps no id→name map of its own.
    renderCard({ ...base, origin: 'pr-doctor', originLabel: 'PR Doctor' })
    const badge = screen.getByTestId('card-provenance')
    expect(badge).toBeInTheDocument()
    expect(badge).toHaveTextContent('Created by PR Doctor')
  })

  it('links to /automation', () => {
    renderCard({ ...base, origin: 'pr-doctor' })
    const badge = screen.getByTestId('card-provenance')
    expect(badge).toHaveAttribute('href', '/automation')
  })

  it('falls back to the raw origin id when no label was published', () => {
    renderCard({ ...base, origin: 'custom-bot' })
    const badge = screen.getByTestId('card-provenance')
    expect(badge).toHaveTextContent('Created by custom-bot')
  })

  it('does not render the badge when origin is absent', () => {
    renderCard(base)
    expect(screen.queryByTestId('card-provenance')).not.toBeInTheDocument()
  })

  it('does not render the badge when origin is null', () => {
    renderCard({ ...base, origin: null })
    expect(screen.queryByTestId('card-provenance')).not.toBeInTheDocument()
  })
})

describe('SessionCard assisted-by badge', () => {
  it('shows a success-toned provenance badge when autopilot is actively guiding the session', () => {
    renderCard({ ...base, assistedBy: 'autopilot', assistedByLabel: 'Autopilot' })
    const badge = screen.getByTestId('card-provenance')
    expect(badge).toHaveTextContent('Assisted by Autopilot')
    expect(badge).toHaveAttribute('title', 'Assisted by automation: autopilot')
    // Recolored from the generic info-toned provenance to success so it reads as
    // "the robot is driving this", consistent with the edge accent + live marker.
    expect(badge.className).toMatch(/success/)
    expect(badge.className).not.toMatch(/info/)
  })

  it('keeps the badge info-toned for an autopilot-assisted session that is paused', () => {
    renderCard({ ...base, assistedBy: 'autopilot', autopilotPaused: true })
    const badge = screen.getByTestId('card-provenance')
    expect(badge.className).toMatch(/info/)
    expect(badge.className).not.toMatch(/success/)
  })

  it('spawned-by takes precedence when both origin and assistedBy are set', () => {
    renderCard({ ...base, origin: 'backlog-runner', assistedBy: 'autopilot' })
    expect(screen.getByTestId('card-provenance')).toHaveTextContent(/Created by/)
  })

  it('no badge when neither origin nor assistedBy is set', () => {
    renderCard(base)
    expect(screen.queryByTestId('card-provenance')).not.toBeInTheDocument()
  })
})

describe('SessionCard maintenance chip', () => {
  // A maintenance session is in the Active list only because it needs a human,
  // so the automation's name sits on line 1 beside the title — not demoted to
  // the line-3 icon marker like ordinary provenance.
  it('names the automation on line 1 for a maintenance-class session', () => {
    renderCard({ ...base, origin: 'pm-classify', originLabel: 'PM Classification', workClass: 'maintenance' })
    const chip = screen.getByTestId('card-maintenance-chip')
    expect(chip).toHaveTextContent('PM Classification')
    expect(chip).toHaveAttribute('title', 'Maintenance session created by PM Classification')
    expect(chip.className).toMatch(/flex-shrink-0/)
  })

  it('falls back to the origin id without a label', () => {
    renderCard({ ...base, origin: 'custom-bot', workClass: 'maintenance' })
    expect(screen.getByTestId('card-maintenance-chip')).toHaveTextContent('custom-bot')
  })

  it('renders no chip for a development-class automation session or a human one', () => {
    renderCard({ ...base, origin: 'backlog-runner', originLabel: 'Backlog Runner', workClass: 'development' })
    expect(screen.queryByTestId('card-maintenance-chip')).not.toBeInTheDocument()
    renderCard({ ...base, id: '2' })
    expect(screen.queryByTestId('card-maintenance-chip')).not.toBeInTheDocument()
  })
})
