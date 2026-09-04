import { render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect } from 'vitest'
import { SessionCard } from '../SessionCard'
import type { Session } from '@/api/types'
import { session } from '@/test/factories'

// Model chip on line 1: which model a session is currently running, readable
// from the list without opening the session. `session.model` is the record's
// chosen alias/pinned id (null = account default); the chip renders it through
// the shared `tierLabel` so the list agrees with the session header and the
// approval panel about what a model is called.

const base: Session = session({
  id: '1',
  name: 'Fix login bug',
  accountName: 'work',
  status: 'in_progress',
  createdAt: '2026-06-22T11:00:00Z',
})

const noop = () => {}

function renderCard(s: Session) {
  return render(
    <MemoryRouter>
      <SessionCard
        session={s}
        account={undefined}
        onOpen={noop}
        onRename={noop}
        onClose={noop}
        onDelete={noop}
        onQuickResume={noop}
      />
    </MemoryRouter>,
  )
}

describe('SessionCard model chip', () => {
  it('shows the tier label for a bare alias', () => {
    renderCard({ ...base, model: 'opus' })
    const chip = screen.getByTestId('card-model')
    expect(chip).toHaveTextContent('Opus')
    expect(chip).toHaveAttribute('title', 'Model: Opus')
  })

  it('shows the friendly full name for a pinned model id', () => {
    renderCard({ ...base, model: 'claude-haiku-4-5' })
    expect(screen.getByTestId('card-model')).toHaveTextContent('Haiku 4.5')
  })

  it('keeps a [1m] context tag legible', () => {
    renderCard({ ...base, model: 'opus[1m]' })
    // tierLabel returns unknown shapes verbatim; the chip must not blank them.
    expect(screen.getByTestId('card-model')).toHaveTextContent('opus[1m]')
  })

  it('renders nothing when the session runs the account default (null model)', () => {
    renderCard({ ...base, model: null })
    expect(screen.queryByTestId('card-model')).not.toBeInTheDocument()
  })

  it('renders nothing when the record predates the model field', () => {
    renderCard(base)
    expect(screen.queryByTestId('card-model')).not.toBeInTheDocument()
  })

  it('lives in line 1 next to the account chip and is wide-card only', () => {
    renderCard({ ...base, model: 'sonnet' })
    const identity = screen.getByTestId('card-identity')
    const chip = within(identity).getByTestId('card-model')
    expect(chip).toHaveClass('hidden', '@md:inline-flex')
    // Model directly precedes the account chip so the pair reads together.
    expect(chip.nextElementSibling).toHaveTextContent('work')
  })

  it('flags a switch queued for the end of the current turn', () => {
    renderCard({ ...base, model: 'opus', modelPending: true })
    expect(screen.getByTestId('card-model')).toHaveAttribute(
      'title',
      'Model: Opus (switching after this turn)',
    )
  })

  it('flags a parked upgrade awaiting approval', () => {
    renderCard({ ...base, model: 'sonnet', pendingModel: 'fable' })
    expect(screen.getByTestId('card-model')).toHaveAttribute(
      'title',
      'Model: Sonnet (Fable requested, awaiting approval)',
    )
  })

  it('tints the gated tier so an expensive session stands out', () => {
    renderCard({ ...base, model: 'fable' })
    expect(screen.getByTestId('card-model').className).toMatch(/violet/)
  })

  it('keeps ordinary tiers on the neutral chip palette', () => {
    renderCard({ ...base, model: 'sonnet' })
    expect(screen.getByTestId('card-model').className).not.toMatch(/violet/)
  })
})
